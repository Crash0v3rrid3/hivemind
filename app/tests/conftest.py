"""Shared fixtures for the Hivemind Python test suite.

Provides:

* ``FakeEmbedder``-based fixtures (small, deterministic, no downloads).
* An in-memory ``FakeRepository`` that faithfully mirrors the real repository's
  observable contract (two-table union, pool over-fetch, hidden-id computation,
  ON CONFLICT import, history chain) so the service and loops can be driven with
  NO database.
* A controllable clock and an ``EventRecord`` factory.
* A temporary hive-directory fixture for the export/import loops.
* Integration fixtures that SKIP automatically when ``DATABASE_URL_TEST`` is
  unset or Postgres is unreachable.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hivemind_app.config import Identity
from hivemind_app.embeddings import FakeEmbedder
from hivemind_app.serialization import EventRecord

# Small dimension keeps unit tests fast; the real schema uses 384 but nothing in
# the unit layer touches the DB, so the dimension is arbitrary here.
UNIT_DIM = 8


# ---------------------------------------------------------------------------
# Embedder / identity / clock.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder(dim=UNIT_DIM)


@pytest.fixture
def identity() -> Identity:
    return Identity(owner_handle="alice", roster=("alice", "bob", "cara"))


class StepClock:
    """A deterministic clock that advances by a fixed step on each call."""

    def __init__(self, start: datetime, step: timedelta = timedelta(seconds=1)) -> None:
        self._now = start
        self._step = step
        self.calls = 0

    def __call__(self) -> datetime:
        current = self._now
        self._now = self._now + self._step
        self.calls += 1
        return current


@pytest.fixture
def clock() -> StepClock:
    return StepClock(datetime(2026, 8, 19, 10, 0, 0, tzinfo=UTC))


# ---------------------------------------------------------------------------
# EventRecord factory.
# ---------------------------------------------------------------------------


@pytest.fixture
def make_record(fake_embedder: FakeEmbedder) -> Callable[..., EventRecord]:
    """Return a factory that builds valid :class:`EventRecord` objects."""

    def _factory(**overrides: object) -> EventRecord:
        title = str(overrides.pop("title", "a title"))
        body = str(overrides.pop("body", "a body"))
        ts = overrides.pop("ts", datetime(2026, 8, 19, 10, 0, 0, tzinfo=UTC))
        created_at = overrides.pop("created_at", ts)
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
            "created_at": created_at,
            "embedding": fake_embedder.embed(f"{title}\n\n{body}"),
        }
        defaults.update(overrides)
        return EventRecord(**defaults)  # type: ignore[arg-type]

    return _factory


# ---------------------------------------------------------------------------
# In-memory fake repository.
# ---------------------------------------------------------------------------


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


class FakeRepository:
    """In-memory stand-in for :class:`hivemind_app.repository.Repository`.

    Mirrors the observable behaviour the service and loops rely on: two-table
    union, per-table pool limits, hidden-id computation, ON CONFLICT import,
    and the supersede history chain. It records the last ``pool`` value handed
    to the candidate fetchers so tests can assert over-fetch.
    """

    def __init__(self) -> None:
        self.local: list[EventRecord] = []
        self.team: list[EventRecord] = []
        self.exported_ids: set[uuid.UUID] = set()
        self.exported_at: dict[uuid.UUID, datetime] = {}
        self.ingested_paths: set[str] = set()
        self.last_recent_pool: int | None = None
        self.last_nearest_pool: int | None = None
        # Optional hook to force a specific nearest-candidate result (for
        # exercising the service's re-rank / dedupe independent of geometry).
        self.nearest_override: list[tuple[EventRecord, float]] | None = None

    # -- writes --------------------------------------------------------------

    async def insert_local(self, record: EventRecord) -> None:
        self.local.append(record)

    async def import_records(self, records: list[EventRecord]) -> int:
        existing = {r.id for r in self.team}
        inserted = 0
        for record in records:
            if record.id in existing:
                continue  # ON CONFLICT (id) DO NOTHING
            self.team.append(record)
            existing.add(record.id)
            inserted += 1
        return inserted

    async def is_file_ingested(self, path: str) -> bool:
        return path in self.ingested_paths

    async def mark_file_ingested(self, path: str) -> None:
        self.ingested_paths.add(path)

    async def list_unexported_local(self) -> list[EventRecord]:
        rows = [r for r in self.local if r.id not in self.exported_ids]
        rows.sort(key=lambda r: r.created_at)
        return rows

    async def mark_exported(self, ids: list[uuid.UUID], now: datetime) -> None:
        for event_id in ids:
            self.exported_ids.add(event_id)
            self.exported_at[event_id] = now

    # -- reads ---------------------------------------------------------------

    async def get_by_id(self, event_id: uuid.UUID) -> EventRecord | None:
        for record in self.local:
            if record.id == event_id:
                return record
        for record in self.team:
            if record.id == event_id:
                return record
        return None

    async def hidden_ids(self) -> set[uuid.UUID]:
        hidden: set[uuid.UUID] = set()
        for record in (*self.local, *self.team):
            if record.supersedes is not None:
                hidden.add(record.supersedes)
            if record.deletes is not None:
                hidden.add(record.deletes)
        return hidden

    def _recent_one(self, table: list[EventRecord], pool: int) -> list[EventRecord]:
        rows = [r for r in table if r.event_type != "tombstone"]
        rows.sort(key=lambda r: r.ts, reverse=True)
        return rows[:pool]

    async def recent_candidates(self, pool: int) -> list[EventRecord]:
        self.last_recent_pool = pool
        merged = self._recent_one(self.local, pool) + self._recent_one(self.team, pool)
        merged.sort(key=lambda r: r.ts, reverse=True)
        return merged[:pool]

    def _nearest_one(
        self, table: list[EventRecord], query_vec: list[float], pool: int
    ) -> list[tuple[EventRecord, float]]:
        scored = [
            (r, 1.0 - _dot(query_vec, r.embedding)) for r in table if r.event_type != "tombstone"
        ]
        scored.sort(key=lambda h: h[1])
        return scored[:pool]

    async def nearest_candidates(
        self, query_vec: list[float], pool: int
    ) -> list[tuple[EventRecord, float]]:
        self.last_nearest_pool = pool
        if self.nearest_override is not None:
            return list(self.nearest_override)
        # Concatenate per-table pools WITHOUT global re-ranking (the service
        # dedupes + re-ranks), exactly like the SQL repository.
        return self._nearest_one(self.local, query_vec, pool) + self._nearest_one(
            self.team, query_vec, pool
        )

    async def history_chain(self, event_id: uuid.UUID) -> list[EventRecord]:
        by_id: dict[uuid.UUID, EventRecord] = {}
        supersedes_of: dict[uuid.UUID, uuid.UUID] = {}
        superseded_by: dict[uuid.UUID, uuid.UUID] = {}
        for record in (*self.local, *self.team):
            by_id[record.id] = record
            if record.supersedes is not None:
                supersedes_of[record.id] = record.supersedes
                superseded_by[record.supersedes] = record.id

        if event_id not in by_id:
            return []

        root = event_id
        seen: set[uuid.UUID] = set()
        while root in supersedes_of and supersedes_of[root] in by_id:
            if root in seen:
                break
            seen.add(root)
            root = supersedes_of[root]

        chain: list[EventRecord] = []
        cur: uuid.UUID | None = root
        visited: set[uuid.UUID] = set()
        while cur is not None and cur in by_id and cur not in visited:
            visited.add(cur)
            chain.append(by_id[cur])
            cur = superseded_by.get(cur)

        chain.sort(key=lambda r: r.created_at)
        return chain


@pytest.fixture
def fake_repo() -> FakeRepository:
    return FakeRepository()


# ---------------------------------------------------------------------------
# Temporary hive directory.
# ---------------------------------------------------------------------------


@pytest.fixture
def hive_root(tmp_path: Path) -> Path:
    root = tmp_path / "hive"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# Integration fixtures (skip when Postgres is unavailable).
# ---------------------------------------------------------------------------


def _test_database_url() -> str | None:
    return os.environ.get("DATABASE_URL_TEST")


@pytest.fixture
def database_url_test() -> str:
    url = _test_database_url()
    if not url:
        pytest.skip("DATABASE_URL_TEST is not set; skipping Postgres integration test")
    return url


@pytest.fixture
async def pg_engine(database_url_test: str) -> AsyncIterator[object]:
    """Yield a live async engine with an initialised, clean schema.

    Skips gracefully if the database cannot be reached. Truncates the three
    tables before yielding so each test starts from a known-empty state.
    """
    from sqlalchemy import text

    from hivemind_app.db import create_engine
    from hivemind_app.repository import init_schema

    engine = create_engine(database_url_test)
    try:
        try:
            await init_schema(engine)
        except Exception as exc:  # pragma: no cover - environment dependent
            await engine.dispose()
            pytest.skip(f"Postgres unreachable for integration test: {exc}")

        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE local_events, team_events, ingested_files"))
        yield engine
    finally:
        await engine.dispose()
