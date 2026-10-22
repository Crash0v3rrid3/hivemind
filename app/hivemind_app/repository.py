"""Async, append-only persistence over Postgres+pgvector OR SQLite+sqlite-vec.

This is the ONLY module that talks to the database, keeping every other layer DB-free
and unit-testable. All writes are inserts (append-only); updates and deletes are new
rows. The backend is chosen by the engine dialect (see :mod:`hivemind_app.db`): the
idempotent :func:`init_schema` creates the pgvector extension + HNSW indexes on
Postgres, or just the tables on SQLite (sqlite-vec is loaded per-connection and search
is brute-force, which is ample at a team's scale).

Candidate fetches (recent / nearest) deliberately do NOT apply metadata filters or
hidden-id removal — that logic lives in :mod:`hivemind_app.service`.
"""

from __future__ import annotations

import struct
import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import (
    CursorResult,
    LargeBinary,
    func,
    literal,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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
    """Create the tables (and, on Postgres, the vector extension + HNSW indexes).

    Postgres: ``CREATE EXTENSION vector`` before ``create_all`` emits the vector-column
    DDL (same transaction), then the HNSW indexes. SQLite: just ``create_all`` — the
    sqlite-vec extension is loaded per-connection and search is brute-force (no index).
    """
    async with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
            for stmt in _HNSW_INDEX_SQL:
                await conn.execute(text(stmt))
        else:
            await conn.run_sync(Base.metadata.create_all)


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
        # Map to the ORM attribute name (DB column is "metadata", which collides with
        # SQLAlchemy's reserved MetaData, so the Python attr is "event_metadata").
        "event_metadata": dict(record.metadata),
        "supersedes": record.supersedes,
        "deletes": record.deletes,
        "ts": record.ts,
        "created_at": record.created_at,
        "embedding": list(record.embedding),
    }


def _dedupe_by_id(records: list[EventRecord]) -> list[EventRecord]:
    """Last-wins dedupe by id, so one multi-row insert never hits the same conflict
    target twice within a single statement."""
    seen: dict[uuid.UUID, EventRecord] = {}
    for record in records:
        seen[record.id] = record
    return list(seen.values())


class Repository:
    """Async data-access object over the two event tables + bookkeeping."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker
        bind = sessionmaker.kw.get("bind")
        self._is_sqlite = bool(bind is not None and bind.dialect.name == "sqlite")

    # -- dialect-aware insert -------------------------------------------------

    def _insert(self, model: Any) -> Any:
        """INSERT construct carrying ``on_conflict_do_nothing`` for the active backend."""
        return sqlite_insert(model) if self._is_sqlite else pg_insert(model)

    async def _insert_team(self, session: AsyncSession, records: list[EventRecord]) -> int:
        """One multi-row INSERT ... ON CONFLICT (id) DO NOTHING into team_events.

        Returns the number of rows actually inserted (conflicts excluded).
        """
        stmt = (
            self._insert(TeamEvent)
            .values([_record_to_values(r) for r in records])
            .on_conflict_do_nothing(index_elements=["id"])
        )
        result = await session.execute(stmt)
        return cast("CursorResult[Any]", result).rowcount or 0

    # -- writes ---------------------------------------------------------------

    async def insert_local(self, record: EventRecord) -> None:
        """Append a record the owner authored into ``local_events``."""
        async with self._sessionmaker() as session, session.begin():
            values = _record_to_values(record)
            values["exported_at"] = None
            await session.execute(self._insert(LocalEvent).values(**values))

    async def import_records(self, records: list[EventRecord]) -> int:
        """Bulk-insert ingested records into ``team_events`` (ON CONFLICT DO NOTHING).

        One multi-row INSERT (deduped by id). Returns rows actually inserted.
        """
        unique = _dedupe_by_id(records)
        if not unique:
            return 0
        async with self._sessionmaker() as session, session.begin():
            return await self._insert_team(session, unique)

    async def is_file_ingested(self, path: str) -> bool:
        async with self._sessionmaker() as session:
            row = await session.get(IngestedFile, path)
            return row is not None

    async def mark_file_ingested(self, path: str) -> None:
        async with self._sessionmaker() as session, session.begin():
            stmt = (
                self._insert(IngestedFile)
                .values(path=path)
                .on_conflict_do_nothing(index_elements=["path"])
            )
            await session.execute(stmt)

    async def import_file(self, records: list[EventRecord], path: str) -> int:
        """Insert a file's records AND mark the file ingested in ONE transaction.

        Batched multi-row insert (deduped by id); the file is always recorded as
        ingested — even when it yields zero new rows — so it is never re-read.
        """
        unique = _dedupe_by_id(records)
        async with self._sessionmaker() as session, session.begin():
            inserted = await self._insert_team(session, unique) if unique else 0
            await session.execute(
                self._insert(IngestedFile)
                .values(path=path)
                .on_conflict_do_nothing(index_elements=["path"])
            )
        return inserted

    async def list_ingested_files(self) -> set[str]:
        """Return every ingested file path in one query (replaces N point gets)."""
        async with self._sessionmaker() as session:
            rows = (await session.execute(select(IngestedFile.path))).scalars().all()
            return set(rows)

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
        """Return up to ``pool`` most-recent records from both tables (by ts desc)."""
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

        Postgres uses pgvector's ``<=>`` (cosine) via ``cosine_distance``; SQLite uses
        sqlite-vec's ``vec_distance_cosine`` against the float32 BLOB. Either way the two
        tables' pools are concatenated; the service unions, drops hidden ids, re-sorts.
        """
        hits: list[tuple[EventRecord, float]] = []
        qblob = struct.pack(f"<{len(query_vec)}f", *query_vec) if self._is_sqlite else None
        async with self._sessionmaker() as session:
            for model in _EVENT_TABLES:
                if self._is_sqlite:
                    distance = func.vec_distance_cosine(
                        model.embedding, literal(qblob, type_=LargeBinary)
                    )
                else:
                    distance = model.embedding.cosine_distance(query_vec)
                stmt = (
                    select(model, distance.label("distance"))
                    .where(model.event_type != "tombstone")
                    .order_by(distance.asc())
                    .limit(pool)
                )
                for row in (await session.execute(stmt)).all():
                    hits.append((_orm_to_record(row[0]), float(row[1])))
        return hits

    async def history_chain(self, event_id: uuid.UUID) -> list[EventRecord]:
        """Return the supersede chain for ``event_id``, ordered by created_at."""
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
