"""SQLite + sqlite-vec backend tests (the lighter, embedded persistence option).

Needs no external server: a temp file DB is created per test and the sqlite-vec
extension is loaded by ``db.create_engine``. The module skips if ``aiosqlite`` or
``sqlite_vec`` are unavailable. Mirrors the Postgres integration coverage (schema
init, append-only CRUD, hidden-id/history, tombstone exclusion, nearest-neighbour
ordering across both tables) plus a tz-aware round-trip check for SQLite.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("sqlite_vec")

from hivemind_app.db import create_engine, create_sessionmaker
from hivemind_app.embeddings import FakeEmbedder, zero_vector
from hivemind_app.repository import Repository, init_schema
from hivemind_app.serialization import EventRecord

EMB = FakeEmbedder(dim=384)


def _rec(**overrides: object) -> EventRecord:
    title = str(overrides.pop("title", "title"))
    body = str(overrides.pop("body", "body"))
    ts = overrides.pop("ts", datetime(2026, 8, 19, 10, 0, 0, tzinfo=UTC))
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "author": "alice",
        "event_type": "update",
        "status": "info",
        "title": title,
        "body": body,
        "jira_project": "BETEL",
        "jira_ticket": None,
        "tags": [],
        "metadata": {},
        "supersedes": None,
        "deletes": None,
        "ts": ts,
        "created_at": ts,
        "embedding": EMB.embed(f"{title}\n\n{body}"),
    }
    defaults.update(overrides)
    return EventRecord(**defaults)  # type: ignore[arg-type]


@pytest.fixture
async def sqlite_engine(tmp_path) -> AsyncIterator[object]:  # type: ignore[no-untyped-def]
    """A fresh SQLite+sqlite-vec engine with an initialised schema (temp file DB)."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'hivemind.db'}")
    await init_schema(engine)
    yield engine
    await engine.dispose()


def _repo(engine) -> Repository:  # type: ignore[no-untyped-def]
    return Repository(create_sessionmaker(engine))


async def test_schema_init_is_idempotent(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    await init_schema(sqlite_engine)
    await init_schema(sqlite_engine)


async def test_insert_and_get_local(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(sqlite_engine)
    rec = _rec(title="hello", tags=["a", "b"], metadata={"k": 1}, jira_ticket="BETEL-1")
    await repo.insert_local(rec)
    got = await repo.get_by_id(rec.id)
    assert got is not None
    assert got.id == rec.id
    assert got.title == "hello"
    assert got.tags == ["a", "b"]
    assert got.metadata == {"k": 1}
    assert got.jira_ticket == "BETEL-1"
    assert len(got.embedding) == 384


async def test_timestamps_roundtrip_tz_aware(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(sqlite_engine)
    rec = _rec()
    await repo.insert_local(rec)
    got = await repo.get_by_id(rec.id)
    assert got is not None
    assert got.ts.tzinfo is not None and got.created_at.tzinfo is not None
    assert got.ts == rec.ts


async def test_unexported_and_mark_exported(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(sqlite_engine)
    rec = _rec()
    await repo.insert_local(rec)
    assert [r.id for r in await repo.list_unexported_local()] == [rec.id]
    await repo.mark_exported([rec.id], datetime.now(UTC))
    assert await repo.list_unexported_local() == []


async def test_import_records_on_conflict(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(sqlite_engine)
    rec = _rec(author="bob")
    assert await repo.import_records([rec]) == 1
    assert await repo.import_records([rec]) == 0
    got = await repo.get_by_id(rec.id)
    assert got is not None and got.author == "bob"


async def test_ingested_file_bookkeeping(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(sqlite_engine)
    path = "bob/2026-08-19/123-abc.ndjson"
    assert await repo.is_file_ingested(path) is False
    await repo.mark_file_ingested(path)
    assert await repo.is_file_ingested(path) is True
    await repo.mark_file_ingested(path)  # idempotent
    assert await repo.list_ingested_files() == {path}


async def test_import_file_inserts_and_marks(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(sqlite_engine)
    rec = _rec(author="bob")
    path = "bob/2026-08-19/1-x.ndjson"
    assert await repo.import_file([rec], path) == 1
    assert await repo.is_file_ingested(path) is True
    assert await repo.import_file([rec], path) == 0  # already ingested id


async def test_hidden_ids_across_tables(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(sqlite_engine)
    original = _rec(title="v1")
    superseder = _rec(title="v2", supersedes=original.id)
    await repo.insert_local(original)
    await repo.insert_local(superseder)
    foreign = _rec(author="bob")
    tomb = _rec(author="bob", event_type="tombstone", deletes=foreign.id, embedding=zero_vector())
    await repo.import_records([foreign, tomb])
    hidden = await repo.hidden_ids()
    assert original.id in hidden
    assert foreign.id in hidden


async def test_recent_candidates_excludes_tombstones(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(sqlite_engine)
    live = _rec(title="live")
    tomb = _rec(title="", event_type="tombstone", deletes=uuid.uuid4(), embedding=zero_vector())
    await repo.insert_local(live)
    await repo.insert_local(tomb)
    ids = {r.id for r in await repo.recent_candidates(pool=200)}
    assert live.id in ids
    assert tomb.id not in ids


async def test_history_chain_ordered(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(sqlite_engine)
    base = datetime(2026, 8, 19, 10, tzinfo=UTC)
    v1 = _rec(title="v1", created_at=base)
    v2 = _rec(title="v2", supersedes=v1.id, created_at=base + timedelta(seconds=1))
    v3 = _rec(title="v3", supersedes=v2.id, created_at=base + timedelta(seconds=2))
    for r in (v1, v2, v3):
        await repo.insert_local(r)
    chain = await repo.history_chain(v2.id)
    assert [r.id for r in chain] == [v1.id, v2.id, v3.id]


async def test_nearest_candidates_orders_by_distance(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(sqlite_engine)
    target = _rec(title="database migration deadlock", body="postgres hang")
    other1 = _rec(title="frontend button color", body="css tweak")
    other2 = _rec(title="release notes typo", body="docs fix")
    for r in (target, other1, other2):
        await repo.insert_local(r)
    query_vec = EMB.embed("database migration deadlock\n\npostgres hang")
    hits = await repo.nearest_candidates(query_vec, pool=200)
    assert hits
    hits.sort(key=lambda h: h[1])
    assert hits[0][0].id == target.id
    assert hits[0][1] == pytest.approx(0.0, abs=1e-4)


async def test_nearest_candidates_spans_both_tables(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(sqlite_engine)
    local_rec = _rec(title="local topic")
    team_rec = _rec(author="bob", title="team topic")
    await repo.insert_local(local_rec)
    await repo.import_records([team_rec])
    hits = await repo.nearest_candidates(EMB.embed("anything"), pool=200)
    ids = {r.id for r, _ in hits}
    assert local_rec.id in ids
    assert team_rec.id in ids
