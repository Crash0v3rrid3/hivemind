"""Postgres integration tests (SPEC §4, §15).

Marked ``integration`` and SKIPPED automatically when ``DATABASE_URL_TEST`` is
unset or Postgres is unreachable (see ``conftest.pg_engine``). Bring up a local
pgvector with ``docker compose -f docker-compose.test.yml up -d`` and point the
tests at it:

    DATABASE_URL_TEST=postgresql+asyncpg://hivemind:hivemind@localhost:5433/hivemind_test \\
        pytest -m integration

Exercises: idempotent schema init (+ HNSW indexes), repository CRUD, append-only
hidden-id / history computation, and HNSW nearest-neighbour ordering. The real
schema uses ``vector(384)`` so a 384-dim ``FakeEmbedder`` is used (no download).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from hivemind_app.db import create_sessionmaker
from hivemind_app.embeddings import FakeEmbedder, zero_vector
from hivemind_app.repository import Repository, init_schema
from hivemind_app.serialization import EventRecord

pytestmark = pytest.mark.integration

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


def _repo(engine) -> Repository:  # type: ignore[no-untyped-def]
    return Repository(create_sessionmaker(engine))


# ---------------------------------------------------------------------------
# Schema init.
# ---------------------------------------------------------------------------


async def test_schema_init_is_idempotent(pg_engine) -> None:  # type: ignore[no-untyped-def]
    # pg_engine already ran init once; running again must not error.
    await init_schema(pg_engine)
    await init_schema(pg_engine)


async def test_hnsw_indexes_exist(pg_engine) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import text

    async with pg_engine.begin() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE indexname IN "
                        "('ix_local_events_embedding_hnsw','ix_team_events_embedding_hnsw')"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert set(rows) == {
        "ix_local_events_embedding_hnsw",
        "ix_team_events_embedding_hnsw",
    }


# ---------------------------------------------------------------------------
# Repository CRUD.
# ---------------------------------------------------------------------------


async def test_insert_and_get_local(pg_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(pg_engine)
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


async def test_unexported_and_mark_exported(pg_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(pg_engine)
    rec = _rec()
    await repo.insert_local(rec)
    pending = await repo.list_unexported_local()
    assert [r.id for r in pending] == [rec.id]

    await repo.mark_exported([rec.id], datetime.now(UTC))
    assert await repo.list_unexported_local() == []


async def test_import_records_on_conflict(pg_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(pg_engine)
    rec = _rec(author="bob")
    assert await repo.import_records([rec]) == 1
    # Re-importing the same id inserts nothing.
    assert await repo.import_records([rec]) == 0
    got = await repo.get_by_id(rec.id)
    assert got is not None
    assert got.author == "bob"


async def test_ingested_file_bookkeeping(pg_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(pg_engine)
    path = "bob/2026-08-19/123-abc.ndjson"
    assert await repo.is_file_ingested(path) is False
    await repo.mark_file_ingested(path)
    assert await repo.is_file_ingested(path) is True
    # Idempotent (ON CONFLICT DO NOTHING).
    await repo.mark_file_ingested(path)
    assert await repo.is_file_ingested(path) is True


# ---------------------------------------------------------------------------
# Append-only: hidden ids + history + tombstone exclusion.
# ---------------------------------------------------------------------------


async def test_hidden_ids_across_tables(pg_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(pg_engine)
    original = _rec(title="v1")
    superseder = _rec(title="v2", supersedes=original.id)
    await repo.insert_local(original)
    await repo.insert_local(superseder)
    # A tombstone in team_events hides a foreign id.
    foreign = _rec(author="bob")
    tomb = _rec(author="bob", event_type="tombstone", deletes=foreign.id, embedding=zero_vector())
    await repo.import_records([foreign, tomb])

    hidden = await repo.hidden_ids()
    assert original.id in hidden
    assert foreign.id in hidden


async def test_recent_candidates_excludes_tombstones(pg_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(pg_engine)
    live = _rec(title="live")
    tomb = _rec(title="", event_type="tombstone", deletes=uuid.uuid4(), embedding=zero_vector())
    await repo.insert_local(live)
    await repo.insert_local(tomb)
    recent = await repo.recent_candidates(pool=200)
    ids = {r.id for r in recent}
    assert live.id in ids
    assert tomb.id not in ids


async def test_history_chain_ordered(pg_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(pg_engine)
    base = datetime(2026, 8, 19, 10, tzinfo=UTC)
    v1 = _rec(title="v1", created_at=base)
    v2 = _rec(title="v2", supersedes=v1.id, created_at=base + timedelta(seconds=1))
    v3 = _rec(title="v3", supersedes=v2.id, created_at=base + timedelta(seconds=2))
    for r in (v1, v2, v3):
        await repo.insert_local(r)
    chain = await repo.history_chain(v2.id)
    assert [r.id for r in chain] == [v1.id, v2.id, v3.id]


# ---------------------------------------------------------------------------
# HNSW nearest-neighbour search.
# ---------------------------------------------------------------------------


async def test_nearest_candidates_orders_by_distance(pg_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(pg_engine)
    target = _rec(title="database migration deadlock", body="postgres hang")
    other1 = _rec(title="frontend button color", body="css tweak")
    other2 = _rec(title="release notes typo", body="docs fix")
    for r in (target, other1, other2):
        await repo.insert_local(r)

    query_vec = EMB.embed("database migration deadlock\n\npostgres hang")
    hits = await repo.nearest_candidates(query_vec, pool=200)
    assert hits, "expected at least one hit"
    # Closest hit must be the identical-text record (distance ~0).
    hits.sort(key=lambda h: h[1])
    assert hits[0][0].id == target.id
    assert hits[0][1] == pytest.approx(0.0, abs=1e-4)


async def test_nearest_candidates_spans_both_tables(pg_engine) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(pg_engine)
    local_rec = _rec(title="local topic")
    team_rec = _rec(author="bob", title="team topic")
    await repo.insert_local(local_rec)
    await repo.import_records([team_rec])
    hits = await repo.nearest_candidates(EMB.embed("anything"), pool=200)
    ids = {r.id for r, _ in hits}
    assert local_rec.id in ids
    assert team_rec.id in ids
