"""Export and import loops (filesystem <-> DB).

The loop *bodies* are exposed as :func:`export_once` and :func:`import_once` so
tests can drive them deterministically without a scheduler. The hive filesystem
root, the clock, and (for export) the file-uuid factory are injected. The async
schedulers (:func:`run_export_loop`, :func:`run_import_loop`) simply call the
``_once`` bodies on an interval until a stop event is set.

Nothing here loads the embedding model or opens the DB at import time.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import structlog

from .config import Identity, resolve_roster, validate_handle
from .serialization import (
    EventRecord,
    atomic_write_text,
    author_from_path,
    batch_filename,
    day_bucket,
    day_dir,
    is_ndjson_file,
    parse_ndjson,
    serialize_records,
)
from .service import RepositoryLike

log = structlog.get_logger(__name__)


class ExportRepository(RepositoryLike, Protocol):  # pragma: no cover - typing aid
    """Extends the read protocol with the export-specific write methods."""

    async def list_unexported_local(self) -> list[EventRecord]: ...

    async def mark_exported(self, ids: list[uuid.UUID], now: datetime) -> None: ...


class ImportRepository(RepositoryLike, Protocol):  # pragma: no cover - typing aid
    """Extends the read protocol with the import-specific write methods."""

    async def list_ingested_files(self) -> set[str]: ...

    async def import_file(
        self, records: list[EventRecord], path: str, *, own: bool = False
    ) -> int: ...

    async def is_file_ingested(self, path: str) -> bool: ...

    async def mark_file_ingested(self, path: str) -> None: ...

    async def import_records(self, records: list[EventRecord]) -> int: ...


def _now(clock: Callable[[], datetime] | None) -> datetime:
    return (clock or (lambda: datetime.now(UTC)))()


# ---------------------------------------------------------------------------
# Export.
# ---------------------------------------------------------------------------


async def export_once(
    repo: ExportRepository,
    owner_handle: str,
    hive_root: str | Path,
    *,
    clock: Callable[[], datetime] | None = None,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> list[str]:
    """Export unexported local records to immutable NDJSON batch files.

    Returns the list of file paths written (empty if nothing to export).
    """
    records = await repo.list_unexported_local()
    if not records:
        return []

    root = Path(hive_root)
    validate_handle(owner_handle)
    if not await asyncio.to_thread(root.is_dir) or not await asyncio.to_thread(
        (root / owner_handle).is_dir
    ):
        raise OSError("hive and provisioned owner folder must exist before export")
    if any(r.author != owner_handle for r in records):
        raise ValueError("refusing export under a different owner identity")
    now = _now(clock)
    epoch_ms = int(now.timestamp() * 1000)

    # Group by UTC day of ``ts``.
    groups: dict[str, list[EventRecord]] = {}
    for record in records:
        groups.setdefault(day_bucket(record.ts), []).append(record)

    written: list[str] = []
    for day, group in sorted(groups.items()):
        directory = day_dir(hive_root, owner_handle, day)
        filename = batch_filename(epoch_ms, uuid_factory())
        path = directory / filename
        await asyncio.to_thread(_publish, root, owner_handle, path, group)
        written.append(str(path))
        log.info(
            "hivemind.export.file",
            day=day,
            count=len(group),
            path=str(path),
        )

    await repo.mark_exported([r.id for r in records], now)
    return written


# ---------------------------------------------------------------------------
# Import.
# ---------------------------------------------------------------------------


def _publish(root: Path, owner: str, path: Path, records: list[EventRecord]) -> None:
    if not root.is_dir() or not (root / owner).is_dir():
        raise OSError("hive unavailable; export remains pending")
    # Create only the day folder: never recreate a vanished mount or owner folder.
    path.parent.mkdir(exist_ok=True)
    atomic_write_text(path, serialize_records(records), create_parents=False)


def _member_folders(hive_root: Path, identity: Identity) -> list[Path]:
    """Immediate subfolders of the hive that are importable members."""
    if not hive_root.is_dir():
        return []
    folders: list[Path] = []
    for child in sorted(hive_root.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        name = child.name
        try:
            validate_handle(name)
        except ValueError:
            continue
        if name != identity.owner_handle and not identity.is_member(name):
            continue  # roster-validated; ignore unknown folders
        folders.append(child)
    return folders


def _ndjson_files(folder: Path) -> list[Path]:
    """All published NDJSON files under a member folder (recursively)."""
    return sorted(
        p
        for p in folder.rglob("*.ndjson")
        if p.is_file()
        and not p.is_symlink()
        and p.resolve().is_relative_to(folder.resolve())
        and is_ndjson_file(p.name)
    )


async def import_once(
    repo: ImportRepository,
    identity: Identity,
    hive_root: str | Path,
) -> int:
    """Import peer files and recover the owner's published local history.

    Returns the number of records newly inserted. Partial or failed files retry.
    Idempotent: already-ingested files are skipped; re-running inserts nothing.
    """
    root = Path(hive_root)
    identity = await asyncio.to_thread(resolve_roster, identity, root)
    inserted_total = 0

    # Fetch the whole ingested set ONCE per cycle instead of a DB point lookup
    # per file. Files are immutable and day-bucketed, so a path already in the set
    # needs no filesystem read and no query.
    ingested = await repo.list_ingested_files()

    for folder in await asyncio.to_thread(_member_folders, root, identity):
        author = folder.name
        try:
            files = await asyncio.to_thread(_ndjson_files, folder)
        except OSError as exc:
            log.warning("hivemind.import.folder_error", path=str(folder), error=str(exc))
            continue
        for file_path in files:
            rel = file_path.relative_to(root).as_posix()
            if rel in ingested:
                continue  # already ingested -> skip without any IO
            # Defensive: derive author from the folder, ignore the file's field.
            assert author_from_path(rel) == author
            try:
                text = await asyncio.to_thread(file_path.read_text, encoding="utf-8")
            except (OSError, UnicodeError) as exc:  # pragma: no cover - filesystem race
                log.warning("hivemind.import.read_error", path=rel, error=str(exc))
                continue  # transient; retried next cycle (not marked)

            try:
                records = await asyncio.to_thread(parse_ndjson, text, require_complete=True)
            except Exception as exc:
                log.warning("hivemind.import.parse_error", path=rel, error=str(exc))
                continue  # retried next cycle (not marked)

            # Stamp the authoritative author from the read-only folder, then insert
            # records + mark the file ingested in a SINGLE transaction.
            stamped = [_with_author(r, author) for r in records]
            try:
                inserted = await repo.import_file(stamped, rel, own=author == identity.owner_handle)
            except Exception as exc:
                log.warning("hivemind.import.persistence_error", path=rel, error=str(exc))
                continue
            ingested.add(rel)
            inserted_total += inserted
            log.info(
                "hivemind.import.file",
                path=rel,
                records=len(stamped),
                inserted=inserted,
            )

    return inserted_total


def _with_author(record: EventRecord, author: str) -> EventRecord:
    """Return a copy of ``record`` with ``author`` overridden."""
    return replace(record, author=author)


# ---------------------------------------------------------------------------
# Async schedulers.
# ---------------------------------------------------------------------------


async def run_export_loop(
    repo: ExportRepository,
    owner_handle: str,
    hive_root: str | Path,
    interval_sec: int,
    *,
    clock: Callable[[], datetime] | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run :func:`export_once` every ``interval_sec`` until stopped."""
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        try:
            await export_once(repo, owner_handle, hive_root, clock=clock)
        except Exception:
            log.exception("hivemind.export.loop_error")
        await _sleep_or_stop(interval_sec, stop)


async def run_import_loop(
    repo: ImportRepository,
    identity: Identity,
    hive_root: str | Path,
    interval_sec: int,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run :func:`import_once` every ``interval_sec`` until stopped."""
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        try:
            await import_once(repo, identity, hive_root)
        except Exception:
            log.exception("hivemind.import.loop_error")
        await _sleep_or_stop(interval_sec, stop)


async def _sleep_or_stop(interval_sec: int, stop: asyncio.Event) -> None:
    """Sleep for ``interval_sec`` or wake early when ``stop`` is set."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=interval_sec)
