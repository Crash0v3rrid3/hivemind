"""Export / import loop bodies driven against a temp hive dir (SPEC §7, §14).

``export_once`` and ``import_once`` are called directly (no scheduler) over the
in-memory ``FakeRepository`` and a ``tmp_path`` hive root.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from hivemind_app.loops import export_once, import_once
from hivemind_app.serialization import (
    is_ndjson_file,
    parse_ndjson,
    serialize_records,
)


def _fixed_clock(dt: datetime):  # type: ignore[no-untyped-def]
    return lambda: dt


def _write_batch(folder: Path, records, name: str = "batch.ndjson") -> Path:  # type: ignore[no-untyped-def]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(serialize_records(records), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# export_once.
# ---------------------------------------------------------------------------


async def test_export_nothing_returns_empty(fake_repo, hive_root) -> None:  # type: ignore[no-untyped-def]
    written = await export_once(fake_repo, "alice", hive_root)
    assert written == []


async def test_export_writes_immutable_file_and_marks_exported(  # type: ignore[no-untyped-def]
    fake_repo, hive_root, make_record
) -> None:
    rec = make_record(ts=datetime(2026, 8, 19, 10, tzinfo=UTC))
    await fake_repo.insert_local(rec)

    written = await export_once(
        fake_repo,
        "alice",
        hive_root,
        clock=_fixed_clock(datetime(2026, 8, 19, 10, 0, 0, tzinfo=UTC)),
        uuid_factory=lambda: uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    assert len(written) == 1
    path = Path(written[0])
    assert path.exists()
    assert is_ndjson_file(path.name)
    # Located under <hive>/alice/<day>/.
    assert path.parent == hive_root / "alice" / "2026-08-19"

    # File content parses back to the same record.
    parsed = parse_ndjson(path.read_text(encoding="utf-8"))
    assert [r.id for r in parsed] == [rec.id]

    # No leftover temp file.
    assert list(path.parent.glob("*.tmp")) == []

    # Record is now marked exported.
    assert await fake_repo.list_unexported_local() == []


async def test_export_groups_by_utc_day(fake_repo, hive_root, make_record) -> None:  # type: ignore[no-untyped-def]
    d19 = make_record(ts=datetime(2026, 8, 19, 23, tzinfo=UTC))
    d20 = make_record(ts=datetime(2026, 8, 20, 1, tzinfo=UTC))
    await fake_repo.insert_local(d19)
    await fake_repo.insert_local(d20)

    written = await export_once(
        fake_repo,
        "alice",
        hive_root,
        clock=_fixed_clock(datetime(2026, 8, 20, 2, tzinfo=UTC)),
    )
    assert len(written) == 2
    days = {Path(p).parent.name for p in written}
    assert days == {"2026-08-19", "2026-08-20"}


async def test_export_is_idempotent_after_marking(  # type: ignore[no-untyped-def]
    fake_repo, hive_root, make_record
) -> None:
    await fake_repo.insert_local(make_record(ts=datetime(2026, 8, 19, 10, tzinfo=UTC)))
    first = await export_once(fake_repo, "alice", hive_root)
    assert len(first) == 1
    # Nothing new to export on a second pass.
    second = await export_once(fake_repo, "alice", hive_root)
    assert second == []


# ---------------------------------------------------------------------------
# import_once.
# ---------------------------------------------------------------------------


async def test_import_reads_peer_folder(  # type: ignore[no-untyped-def]
    fake_repo, identity, hive_root, make_record
) -> None:
    rec = make_record(author="bob")
    _write_batch(hive_root / "bob" / "2026-08-19", [rec])
    inserted = await import_once(fake_repo, identity, hive_root)
    assert inserted == 1
    assert len(fake_repo.team) == 1
    assert fake_repo.team[0].id == rec.id


async def test_import_stamps_author_from_folder(  # type: ignore[no-untyped-def]
    fake_repo, identity, hive_root, make_record
) -> None:
    # File claims a different author; the folder is authoritative.
    rec = make_record(author="impostor")
    _write_batch(hive_root / "bob" / "2026-08-19", [rec])
    await import_once(fake_repo, identity, hive_root)
    assert fake_repo.team[0].author == "bob"


async def test_import_is_idempotent(  # type: ignore[no-untyped-def]
    fake_repo, identity, hive_root, make_record
) -> None:
    _write_batch(hive_root / "bob" / "2026-08-19", [make_record(author="bob")])
    first = await import_once(fake_repo, identity, hive_root)
    second = await import_once(fake_repo, identity, hive_root)
    assert first == 1
    assert second == 0  # already-ingested file skipped
    assert len(fake_repo.team) == 1


async def test_import_on_conflict_does_nothing(  # type: ignore[no-untyped-def]
    fake_repo, identity, hive_root, make_record
) -> None:
    rec = make_record(author="bob")
    # Same id already present in team from a previous distinct file.
    await fake_repo.import_records([rec])
    _write_batch(hive_root / "bob" / "2026-08-19", [rec], name="another.ndjson")
    inserted = await import_once(fake_repo, identity, hive_root)
    assert inserted == 0  # ON CONFLICT (id) DO NOTHING
    assert len(fake_repo.team) == 1


async def test_import_skips_own_folder(  # type: ignore[no-untyped-def]
    fake_repo, identity, hive_root, make_record
) -> None:
    _write_batch(hive_root / "alice" / "2026-08-19", [make_record(author="alice")])
    inserted = await import_once(fake_repo, identity, hive_root)
    assert inserted == 0
    assert fake_repo.team == []


async def test_import_filters_unknown_folder(  # type: ignore[no-untyped-def]
    fake_repo, identity, hive_root, make_record
) -> None:
    # mallory is not in the roster (alice, bob, cara).
    _write_batch(hive_root / "mallory" / "2026-08-19", [make_record(author="mallory")])
    inserted = await import_once(fake_repo, identity, hive_root)
    assert inserted == 0
    assert fake_repo.team == []


async def test_import_ignores_tmp_files(  # type: ignore[no-untyped-def]
    fake_repo, identity, hive_root, make_record
) -> None:
    folder = hive_root / "bob" / "2026-08-19"
    folder.mkdir(parents=True)
    # A half-written temp file must be ignored entirely.
    (folder / "wip.ndjson.tmp").write_text(
        serialize_records([make_record(author="bob")]), encoding="utf-8"
    )
    inserted = await import_once(fake_repo, identity, hive_root)
    assert inserted == 0
    assert fake_repo.team == []


async def test_import_tolerates_partial_trailing_line(  # type: ignore[no-untyped-def]
    fake_repo, identity, hive_root, make_record
) -> None:
    good = make_record(author="bob")
    folder = hive_root / "bob" / "2026-08-19"
    folder.mkdir(parents=True)
    path = folder / "batch.ndjson"
    # One complete line + a trailing incomplete (unterminated) JSON fragment.
    text = serialize_records([good]) + '{"id": "half-written'
    path.write_text(text, encoding="utf-8")

    inserted = await import_once(fake_repo, identity, hive_root)
    assert inserted == 1
    assert fake_repo.team[0].id == good.id
    # File is still recorded as ingested (won't be reprocessed).
    rel = path.relative_to(hive_root).as_posix()
    assert await fake_repo.is_file_ingested(rel)


async def test_import_multiple_members(  # type: ignore[no-untyped-def]
    fake_repo, identity, hive_root, make_record
) -> None:
    _write_batch(hive_root / "bob" / "2026-08-19", [make_record(author="bob")])
    _write_batch(hive_root / "cara" / "2026-08-19", [make_record(author="cara")])
    inserted = await import_once(fake_repo, identity, hive_root)
    assert inserted == 2
    authors = {r.author for r in fake_repo.team}
    assert authors == {"bob", "cara"}


async def test_import_uses_one_bulk_ingested_query(  # type: ignore[no-untyped-def]
    fake_repo, identity, hive_root, make_record
) -> None:
    # The hot path must issue ONE ingested-set query per cycle and ZERO per-file
    # point lookups (the old O(files) query pattern).
    calls = {"list": 0, "per_file": 0}
    orig_list = fake_repo.list_ingested_files
    orig_is = fake_repo.is_file_ingested

    async def counting_list() -> set[str]:
        calls["list"] += 1
        return await orig_list()

    async def counting_is(path: str) -> bool:
        calls["per_file"] += 1
        return await orig_is(path)

    fake_repo.list_ingested_files = counting_list
    fake_repo.is_file_ingested = counting_is

    _write_batch(hive_root / "bob" / "2026-08-19", [make_record(author="bob")])
    _write_batch(hive_root / "bob" / "2026-08-20", [make_record(author="bob")])
    inserted = await import_once(fake_repo, identity, hive_root)

    assert inserted == 2
    assert calls["list"] == 1  # one bulk query for the whole cycle
    assert calls["per_file"] == 0  # no per-file point lookups


async def test_import_missing_hive_root_is_noop(fake_repo, identity, tmp_path) -> None:  # type: ignore[no-untyped-def]
    missing = tmp_path / "does-not-exist"
    inserted = await import_once(fake_repo, identity, missing)
    assert inserted == 0
