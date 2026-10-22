"""NDJSON (de)serialization, atomic file publish, and path helpers.

A batch file lives at ``<hive>/<handle>/<YYYY-MM-DD>/<epoch_ms>-<uuid4>.ndjson``
where the day is the UTC calendar day of the record ``ts``. Files are immutable
and written atomically (write ``.tmp``, flush + fsync, ``os.rename``). Parsing
is tolerant: blank lines and a trailing incomplete line are ignored.

Everything here is pure / filesystem-only and import-safe — no DB, no network.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# On-disk NDJSON format version. The file format is FROZEN within a major
# version: changes are additive only (new optional keys), so a record written
# by any v1.x node is readable by every other v1.x node. Readers ignore unknown
# keys (forward-compatible), and a record with no ``schema_version`` is treated
# as v1. Bump this only for a breaking format change, which is a new major.
SCHEMA_VERSION = 1

# Exact ordered key set for each NDJSON line (matches SPEC §5).
LINE_KEYS: tuple[str, ...] = (
    "schema_version",
    "id",
    "author",
    "event_type",
    "status",
    "title",
    "body",
    "jira_project",
    "jira_ticket",
    "tags",
    "metadata",
    "supersedes",
    "deletes",
    "ts",
    "created_at",
    "embedding",
)


@dataclass(frozen=True)
class EventRecord:
    """A single, fully-materialized memory record.

    This is the in-memory shape shared between the DB layer and the file layer.
    ``embedding`` is carried in files so importers never re-embed.
    """

    id: uuid.UUID
    author: str
    event_type: str
    status: str
    title: str
    body: str
    jira_project: str
    ts: datetime
    created_at: datetime
    embedding: list[float]
    jira_ticket: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    supersedes: uuid.UUID | None = None
    deletes: uuid.UUID | None = None


# ---------------------------------------------------------------------------
# datetime helpers (all times are UTC).
# ---------------------------------------------------------------------------


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def dt_to_iso(dt: datetime) -> str:
    """Serialize a datetime as ISO-8601 UTC with a ``Z`` suffix."""
    u = _to_utc(dt)
    # Drop the ``+00:00`` offset in favour of ``Z``.
    return u.isoformat().replace("+00:00", "Z")


def iso_to_dt(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (``Z`` or offset) into an aware UTC datetime."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return _to_utc(dt)


def day_bucket(ts: datetime) -> str:
    """Return the UTC calendar day of ``ts`` as ``YYYY-MM-DD``."""
    return _to_utc(ts).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Record <-> dict <-> JSON line.
# ---------------------------------------------------------------------------


def record_to_dict(record: EventRecord) -> dict[str, Any]:
    """Convert an :class:`EventRecord` to a JSON-ready dict with all LINE_KEYS."""
    return {
        "schema_version": SCHEMA_VERSION,
        "id": str(record.id),
        "author": record.author,
        "event_type": record.event_type,
        "status": record.status,
        "title": record.title,
        "body": record.body,
        "jira_project": record.jira_project,
        "jira_ticket": record.jira_ticket,
        "tags": list(record.tags),
        "metadata": dict(record.metadata),
        "supersedes": str(record.supersedes) if record.supersedes else None,
        "deletes": str(record.deletes) if record.deletes else None,
        "ts": dt_to_iso(record.ts),
        "created_at": dt_to_iso(record.created_at),
        "embedding": [float(x) for x in record.embedding],
    }


def dict_to_record(obj: dict[str, Any]) -> EventRecord:
    """Build an :class:`EventRecord` from a parsed NDJSON object.

    Unknown keys are ignored, so records from a newer (but same-major) writer
    read cleanly. ``schema_version`` is advisory: a missing value means v1.

    Raises ``KeyError``/``ValueError`` if the object is malformed.
    """
    return EventRecord(
        id=uuid.UUID(str(obj["id"])),
        author=str(obj["author"]),
        event_type=str(obj["event_type"]),
        status=str(obj["status"]),
        title=str(obj["title"]),
        body=str(obj["body"]),
        jira_project=str(obj["jira_project"]),
        jira_ticket=(None if obj.get("jira_ticket") is None else str(obj["jira_ticket"])),
        tags=[str(t) for t in (obj.get("tags") or [])],
        metadata=dict(obj.get("metadata") or {}),
        supersedes=(None if obj.get("supersedes") is None else uuid.UUID(str(obj["supersedes"]))),
        deletes=(None if obj.get("deletes") is None else uuid.UUID(str(obj["deletes"]))),
        ts=iso_to_dt(str(obj["ts"])),
        created_at=iso_to_dt(str(obj["created_at"])),
        embedding=[float(x) for x in obj["embedding"]],
    )


def record_to_line(record: EventRecord) -> str:
    """Serialize a record to a single compact JSON line (no trailing newline)."""
    return json.dumps(record_to_dict(record), separators=(",", ":"), ensure_ascii=False)


def serialize_records(records: list[EventRecord]) -> str:
    """Serialize records to NDJSON text (one object per line, trailing newline)."""
    if not records:
        return ""
    return "".join(record_to_line(r) + "\n" for r in records)


def parse_ndjson(text: str) -> list[EventRecord]:
    """Tolerantly parse NDJSON text into records.

    Blank lines are skipped. A trailing line without a terminating newline is
    treated as potentially incomplete and is skipped if it does not parse as a
    complete JSON object. Lines that parse but are missing required keys raise.
    """
    if not text:
        return []

    # Split preserving knowledge of whether the final line was terminated.
    lines = text.split("\n")
    # If the text ends with "\n", the final split element is "" (complete file).
    trailing_complete = text.endswith("\n")

    records: list[EventRecord] = []
    last_index = len(lines) - 1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        is_last_unterminated = i == last_index and not trailing_complete
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            if is_last_unterminated:
                # Ignore an incomplete trailing line.
                continue
            raise
        records.append(dict_to_record(obj))
    return records


# ---------------------------------------------------------------------------
# Atomic file publish.
# ---------------------------------------------------------------------------


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    """Atomically write ``text`` to ``path``.

    Writes to ``<path>.tmp``, flushes + fsyncs, then ``os.rename`` onto the
    final path (atomic on POSIX). The parent directory is created if needed.
    """
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.rename(tmp, dest)


# ---------------------------------------------------------------------------
# Path helpers.
# ---------------------------------------------------------------------------


def batch_filename(epoch_ms: int, file_uuid: uuid.UUID | None = None) -> str:
    """Return an immutable batch filename ``<epoch_ms>-<uuid4>.ndjson``."""
    fid = file_uuid if file_uuid is not None else uuid.uuid4()
    return f"{epoch_ms}-{fid}.ndjson"


def day_dir(hive_root: str | os.PathLike[str], handle: str, day: str) -> Path:
    """Return ``<hive>/<handle>/<day>/`` as a Path (not created)."""
    return Path(hive_root) / handle / day


def author_from_path(relpath: str | os.PathLike[str]) -> str:
    """Derive the author handle from a hive-relative path.

    The first path component is the owning folder = the authoritative author,
    e.g. ``bob/2026-08-19/123-....ndjson`` -> ``bob``.
    """
    parts = Path(relpath).parts
    if not parts:
        raise ValueError(f"cannot derive author from empty path {relpath!r}")
    return parts[0]


def is_ndjson_file(name: str) -> bool:
    """True for a published (non-temp) NDJSON file name."""
    return name.endswith(".ndjson") and not name.endswith(".tmp")
