"""Async, append-only persistence over Postgres.

This is the ONLY module that talks to the database, keeping every other layer
DB-free and unit-testable. All writes are inserts (append-only); updates and
deletes are new rows. The idempotent :func:`init_schema` creates the pgvector
extension, the tables, and the HNSW indexes.

Candidate fetches (recent / nearest) deliberately do NOT apply metadata filters
or hidden-id removal — that logic lives in :mod:`hivemind_app.service` as pure,
testable functions. The repository's job is the SQL: ordering, nearest-neighbour
search, over-fetch, CRUD, and hidden-id / history computation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .db import Base
from .models import IngestedFile, LocalEvent, TeamEvent
from .serialization import EventRecord

# Both event tables share an identical column set.
_EVENT_TABLES = (LocalEvent, TeamEvent)


# ---------------------------------------------------------------------------
# Schema init (idempotent).
# ---------------------------------------------------------------------------

_HNSW_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_local_events_embedding_hnsw "
    "ON local_events USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS ix_team_events_embedding_hnsw "
    "ON team_events USING hnsw (embedding vector_cosine_ops)",
)


async def init_schema(engine: AsyncEngine) -> None:
    """Create the extension, tables, and HNSW indexes idempotently.

    Safe on a brand-new database: the ``vector`` extension is created before
    ``create_all`` emits the vector-column DDL, all in one transaction.
    """
    async with engine.begin() as conn:
        # CREATE EXTENSION first so the vector type exists before create_all emits
        # the vector(384) column DDL (same transaction — the type is visible).
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        for stmt in _HNSW_INDEX_SQL:
            await conn.execute(text(stmt))


# ---------------------------------------------------------------------------
# ORM <-> EventRecord mapping.
# ---------------------------------------------------------------------------


def _orm_to_record(row: LocalEvent | TeamEvent) -> EventRecord:
    return EventRecord(
        id=row.id,
        author=row.author,
        event_type=row.event_type,
        status=row.status,
        title=row.title,
        body=row.body,
        jira_project=row.jira_project,
        jira_ticket=row.jira_ticket,
        tags=list(row.tags),
        metadata=dict(row.event_metadata),
        supersedes=row.supersedes,
        deletes=row.deletes,
        ts=row.ts,
        created_at=row.created_at,
        embedding=list(row.embedding),
    )


def _record_to_values(record: EventRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "author": record.author,
        "event_type": record.event_type,
        "status": record.status,
        "title": record.title,
        "body": record.body,
        "jira_project": record.jira_project,
        "jira_ticket": record.jira_ticket,
        "tags": list(record.tags),
        # Map to the ORM attribute name (the DB column is "metadata", but the
        # bare key "metadata" collides with SQLAlchemy's reserved MetaData).
        "event_metadata": dict(record.metadata),
        "supersedes": record.supersedes,
        "deletes": record.deletes,
        "ts": record.ts,
        "created_at": record.created_at,
        "embedding": list(record.embedding),
    }


class Repository:
    """Async data-access object over the two event tables + bookkeeping."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    # -- writes ---------------------------------------------------------------

    async def insert_local(self, record: EventRecord) -> None:
        """Append a record the owner authored into ``local_events``."""
        async with self._sessionmaker() as session, session.begin():
            values = _record_to_values(record)
            values["exported_at"] = None
            await session.execute(pg_insert(LocalEvent).values(**values))

    async def import_records(self, records: list[EventRecord]) -> int:
        """Insert ingested records into ``team_events`` (ON CONFLICT DO NOTHING).

        Returns the number of rows actually inserted (new ids).
        """
        if not records:
            return 0
        inserted = 0
        async with self._sessionmaker() as session, session.begin():
            for record in records:
                stmt = (
                    pg_insert(TeamEvent)
                    .values(**_record_to_values(record))
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                result = await session.execute(stmt)
                inserted += cast("CursorResult[Any]", result).rowcount or 0
        return inserted

    async def is_file_ingested(self, path: str) -> bool:
        async with self._sessionmaker() as session:
            row = await session.get(IngestedFile, path)
            return row is not None

    async def mark_file_ingested(self, path: str) -> None:
        async with self._sessionmaker() as session, session.begin():
            stmt = (
                pg_insert(IngestedFile)
                .values(path=path)
                .on_conflict_do_nothing(index_elements=["path"])
            )
            await session.execute(stmt)

    async def list_unexported_local(self) -> list[EventRecord]:
        """Return unexported local records ordered by ``created_at`` ascending."""
        async with self._sessionmaker() as session:
            stmt = (
                select(LocalEvent)
                .where(LocalEvent.exported_at.is_(None))
                .order_by(LocalEvent.created_at.asc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_orm_to_record(r) for r in rows]

    async def mark_exported(self, ids: list[uuid.UUID], now: datetime) -> None:
        if not ids:
            return
        async with self._sessionmaker() as session, session.begin():
            stmt = update(LocalEvent).where(LocalEvent.id.in_(ids)).values(exported_at=now)
            await session.execute(stmt)

    # -- reads ----------------------------------------------------------------

    async def get_by_id(self, event_id: uuid.UUID) -> EventRecord | None:
        """Return a record by id from either table (local takes precedence)."""
        async with self._sessionmaker() as session:
            row = await session.get(LocalEvent, event_id)
            if row is not None:
                return _orm_to_record(row)
            trow = await session.get(TeamEvent, event_id)
            return _orm_to_record(trow) if trow is not None else None

    async def hidden_ids(self) -> set[uuid.UUID]:
        """Union of all non-null ``supersedes`` and ``deletes`` across tables."""
        hidden: set[uuid.UUID] = set()
        async with self._sessionmaker() as session:
            for model in _EVENT_TABLES:
                for col in (model.supersedes, model.deletes):
                    stmt = select(col).where(col.is_not(None))
                    for value in (await session.execute(stmt)).scalars().all():
                        if value is not None:
                            hidden.add(value)
        return hidden

    async def recent_candidates(self, pool: int) -> list[EventRecord]:
        """Return up to ``pool`` most-recent records from both tables (by ts).

        Tombstones are excluded in SQL; hidden-id and metadata filtering are the
        service's responsibility. The union is globally ordered by ``ts`` desc.
        """
        records: list[EventRecord] = []
        async with self._sessionmaker() as session:
            for model in _EVENT_TABLES:
                per = (
                    select(model)
                    .where(model.event_type != "tombstone")
                    .order_by(model.ts.desc())
                    .limit(pool)
                )
                rows = cast(
                    "list[LocalEvent | TeamEvent]",
                    (await session.execute(per)).scalars().all(),
                )
                records.extend(_orm_to_record(r) for r in rows)
        records.sort(key=lambda r: r.ts, reverse=True)
        return records[:pool]

    async def nearest_candidates(
        self, query_vec: list[float], pool: int
    ) -> list[tuple[EventRecord, float]]:
        """Return up to ``pool`` nearest records per table with cosine distance.

        The two tables' candidate pools are concatenated (not re-ranked); the
        service unions, drops hidden ids, re-sorts by distance, and truncates.
        """
        hits: list[tuple[EventRecord, float]] = []
        async with self._sessionmaker() as session:
            for model in _EVENT_TABLES:
                distance = model.embedding.cosine_distance(query_vec)
                stmt = (
                    select(model, distance.label("distance"))
                    .where(model.event_type != "tombstone")
                    .order_by(distance.asc())
                    .limit(pool)
                )
                for row in (await session.execute(stmt)).all():
                    orm_obj = row[0]
                    dist = float(row[1])
                    hits.append((_orm_to_record(orm_obj), dist))
        return hits

    async def history_chain(self, event_id: uuid.UUID) -> list[EventRecord]:
        """Return the supersede chain for ``event_id``, ordered by created_at.

        Walks backwards to the chain root via ``supersedes`` and forwards via
        records that supersede members, across both tables.
        """
        async with self._sessionmaker() as session:
            by_id: dict[uuid.UUID, EventRecord] = {}
            supersedes_of: dict[uuid.UUID, uuid.UUID] = {}
            superseded_by: dict[uuid.UUID, uuid.UUID] = {}
            for model in _EVENT_TABLES:
                rows = cast(
                    "list[LocalEvent | TeamEvent]",
                    (await session.execute(select(model))).scalars().all(),
                )
                for r in rows:
                    rec = _orm_to_record(r)
                    by_id[rec.id] = rec
                    if rec.supersedes is not None:
                        supersedes_of[rec.id] = rec.supersedes
                        superseded_by[rec.supersedes] = rec.id

        if event_id not in by_id:
            return []

        # Walk to the chain root.
        root = event_id
        seen: set[uuid.UUID] = set()
        while root in supersedes_of and supersedes_of[root] in by_id:
            if root in seen:
                break
            seen.add(root)
            root = supersedes_of[root]

        # Walk forward collecting the chain.
        chain: list[EventRecord] = []
        cur: uuid.UUID | None = root
        visited: set[uuid.UUID] = set()
        while cur is not None and cur in by_id and cur not in visited:
            visited.add(cur)
            chain.append(by_id[cur])
            cur = superseded_by.get(cur)

        chain.sort(key=lambda r: r.created_at)
        return chain
