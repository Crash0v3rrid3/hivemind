"""Async, append-only persistence over Postgres+pgvector OR SQLite+sqlite-vec.

This is the ONLY module that talks to the database, keeping every other layer DB-free
and unit-testable. All writes are inserts (append-only); updates and deletes are new
rows. The backend is chosen by the engine dialect (see :mod:`hivemind_app.db`): the
idempotent :func:`init_schema` creates the pgvector extension + HNSW indexes on
Postgres, or just the tables on SQLite (sqlite-vec is loaded per-connection and search
is brute-force, which is ample at a team's scale).

Candidate fetches apply ownership-aware visibility and metadata filters before limits.
"""

from __future__ import annotations

import json
import struct
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import (
    CursorResult,
    LargeBinary,
    func,
    literal,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from .config import DEFAULT_EMBEDDING_DIM
from .db import Base
from .models import ConflictedEvent, IngestedFile, LocalEvent, StateBinding, TeamEvent
from .schemas import SearchFilters
from .serialization import EventRecord, record_to_dict, validate_embedding

# Both event tables share an identical column set.
_EVENT_TABLES: tuple[Any, ...] = (LocalEvent, TeamEvent)


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


def _fingerprint(record: EventRecord) -> str:
    data = record_to_dict(record)
    # Stored vectors are float32 on both backends. Compare their wire precision.
    data["embedding"] = list(
        struct.unpack(
            f"<{len(record.embedding)}f",
            struct.pack(f"<{len(record.embedding)}f", *record.embedding),
        )
    )
    data.pop("embedding_model", None)
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


class Repository:
    """Async data-access object over the two event tables + bookkeeping."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker
        self.embedding_model: str | None = None
        self.owner_handle: str | None = None
        bind = sessionmaker.kw.get("bind")
        self._is_sqlite = bool(bind is not None and bind.dialect.name == "sqlite")

    # -- dialect-aware insert -------------------------------------------------

    def _insert(self, model: Any) -> Any:
        """INSERT construct carrying ``on_conflict_do_nothing`` for the active backend."""
        return sqlite_insert(model) if self._is_sqlite else pg_insert(model)

    async def bind_identity(self, owner: str, hive_id: str, embedding_model: str) -> None:
        """Bind an empty DB, or validate its existing immutable identity.

        Legacy nonempty databases require an explicit migration rather than guessing
        their hive or embedding provenance.
        """
        expected = {"owner": owner, "hive_id": hive_id, "embedding_model": embedding_model}
        async with self._sessionmaker() as session, session.begin():
            existing = dict(
                (await session.execute(select(StateBinding.key, StateBinding.value))).all()
            )
            if not existing:
                for table in _EVENT_TABLES:
                    if await session.scalar(select(func.count()).select_from(table)):
                        raise ValueError(
                            "unbound nonempty database requires explicit identity/model migration"
                        )
                for key, value in expected.items():
                    await session.execute(
                        self._insert(StateBinding)
                        .values(key=key, value=value)
                        .on_conflict_do_nothing(index_elements=["key"])
                    )
                existing = dict(
                    (await session.execute(select(StateBinding.key, StateBinding.value))).all()
                )
            if existing != expected:
                raise ValueError(
                    "database owner, hive or embedding model differs; "
                    "use a fresh database or migrate explicitly"
                )
        self.embedding_model = embedding_model
        self.owner_handle = owner

    def _record(self, row: LocalEvent | TeamEvent) -> EventRecord:
        return replace(_orm_to_record(row), embedding_model=self.embedding_model)

    async def _insert_team(
        self, session: AsyncSession, records: list[EventRecord], *, own: bool = False
    ) -> int:
        """Bounded inserts and persistent, arrival-order-independent UUID quarantine."""
        inserted = 0
        model = LocalEvent if own else TeamEvent
        for start in range(0, len(records), 400):
            batch = records[start : start + 400]
            ids = [r.id for r in batch]
            known: dict[uuid.UUID, EventRecord] = {}
            row: Any
            for table in _EVENT_TABLES:
                for row in (
                    await session.execute(select(table).where(table.id.in_(ids)))
                ).scalars():
                    known[row.id] = self._record(row)
            pending: dict[uuid.UUID, EventRecord] = {}
            for rec in batch:
                validate_embedding(rec.embedding, DEFAULT_EMBEDDING_DIM)
                if self.embedding_model is not None:
                    wire_model = rec.embedding_model
                    if wire_model != self.embedding_model:
                        raise ValueError("imported embedding model does not match database")
                old = known.get(rec.id) or pending.get(rec.id)
                if old is not None:
                    if _fingerprint(old) != _fingerprint(rec):
                        await session.execute(
                            self._insert(ConflictedEvent)
                            .values(id=rec.id)
                            .on_conflict_do_nothing(index_elements=["id"])
                        )
                    continue
                pending[rec.id] = rec
            if pending:
                values = [_record_to_values(r) for r in pending.values()]
                if own:
                    for value in values:
                        value["exported_at"] = datetime.now(UTC)
                result = await session.execute(
                    self._insert(model).values(values).on_conflict_do_nothing(index_elements=["id"])
                )
                inserted += cast("CursorResult[Any]", result).rowcount or 0
        return inserted

    # -- writes ---------------------------------------------------------------

    async def insert_local(self, record: EventRecord) -> None:
        """Append a record the owner authored into ``local_events``."""
        async with self._sessionmaker() as session, session.begin():
            if self.owner_handle is not None and record.author != self.owner_handle:
                raise ValueError("local author differs from bound owner")
            if self.embedding_model is not None and record.embedding_model != self.embedding_model:
                raise ValueError("local embedding model differs from bound model")
            values = _record_to_values(record)
            values["exported_at"] = None
            await session.execute(self._insert(LocalEvent).values(**values))

    async def import_records(self, records: list[EventRecord]) -> int:
        """Import bounded batches, quarantining inconsistent duplicate UUIDs.

        Returns rows newly inserted; identical retries are idempotent.
        """
        unique = records
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

    async def import_file(self, records: list[EventRecord], path: str, *, own: bool = False) -> int:
        """Insert a file's records AND mark the file ingested in ONE transaction.

        The caller validates complete publication before invoking this transaction.
        Owner recovery inserts published records into local_events as exported.
        """
        unique = records
        async with self._sessionmaker() as session, session.begin():
            inserted = await self._insert_team(session, unique, own=own) if unique else 0
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
        """Return at most 1000 unexported, unquarantined local records."""
        async with self._sessionmaker() as session:
            stmt = (
                select(LocalEvent)
                .where(
                    LocalEvent.exported_at.is_(None),
                    LocalEvent.id.not_in(select(ConflictedEvent.id)),
                )
                .order_by(LocalEvent.created_at.asc())
                .limit(1000)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._record(r) for r in rows]

    async def mark_exported(self, ids: list[uuid.UUID], now: datetime) -> None:
        if not ids:
            return
        async with self._sessionmaker() as session, session.begin():
            for start in range(0, len(ids), 400):
                stmt = (
                    update(LocalEvent)
                    .where(LocalEvent.id.in_(ids[start : start + 400]))
                    .values(exported_at=now)
                )
                await session.execute(stmt)

    # -- reads ----------------------------------------------------------------

    async def _all_records(self) -> dict[uuid.UUID, EventRecord]:
        async with self._sessionmaker() as session:
            conflicts = set((await session.execute(select(ConflictedEvent.id))).scalars())
            records: dict[uuid.UUID, EventRecord] = {}
            row: Any
            for model in reversed(_EVENT_TABLES):
                for row in (await session.execute(select(model))).scalars():
                    if row.id not in conflicts:
                        records[row.id] = self._record(row)
            return records

    async def get_by_id(self, event_id: uuid.UUID) -> EventRecord | None:
        async with self._sessionmaker() as session:
            if await session.get(ConflictedEvent, event_id) is not None:
                return None
            for table in _EVENT_TABLES:
                row = await session.get(table, event_id)
                if row is not None:
                    return self._record(row)
        return None

    def _visible_conditions(self, model: Any) -> list[Any]:
        conditions = [model.event_type != "tombstone", model.id.not_in(select(ConflictedEvent.id))]
        if model is TeamEvent:
            conditions.append(model.id.not_in(select(LocalEvent.id)))
        for table in _EVENT_TABLES:
            ref = aliased(table)
            authorized = (
                select(ref.id)
                .where(
                    or_(ref.supersedes == model.id, ref.deletes == model.id),
                    ref.author == model.author,
                    ref.id != model.id,
                    ref.id.not_in(select(ConflictedEvent.id)),
                )
                .correlate(model)
                .exists()
            )
            conditions.append(~authorized)
        return conditions

    def _filter_conditions(self, model: Any, filters: SearchFilters | None) -> list[Any]:
        if filters is None:
            return []
        conditions: list[Any] = []
        for field in ("author", "event_type", "status", "jira_project", "jira_ticket"):
            value = getattr(filters, field)
            if value is not None:
                conditions.append(getattr(model, field) == value)
        if filters.date_from is not None:
            conditions.append(model.ts >= filters.date_from)
        if filters.date_to is not None:
            conditions.append(model.ts <= filters.date_to)
        for tag in filters.tags or []:
            if self._is_sqlite:
                tags = func.json_each(model.tags).table_valued("value")
                conditions.append(
                    select(tags.c.value).where(tags.c.value == tag).correlate(model).exists()
                )
            else:
                conditions.append(model.tags.any(tag))
        return conditions

    async def hidden_ids(self) -> set[uuid.UUID]:
        hidden: set[uuid.UUID] = set()
        async with self._sessionmaker() as session:
            for table in _EVENT_TABLES:
                for target_table in _EVENT_TABLES:
                    ref = aliased(table)
                    target = aliased(target_table)
                    stmt = (
                        select(target.id)
                        .join(ref, or_(ref.supersedes == target.id, ref.deletes == target.id))
                        .where(
                            ref.author == target.author,
                            ref.id != target.id,
                            ref.id.not_in(select(ConflictedEvent.id)),
                            target.id.not_in(select(ConflictedEvent.id)),
                        )
                    )
                    hidden.update((await session.execute(stmt)).scalars())
        return hidden

    async def recent_candidates(
        self,
        pool: int | None,
        *,
        filters: SearchFilters | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[EventRecord]:
        records: list[EventRecord] = []
        row: Any
        async with self._sessionmaker() as session:
            for model in _EVENT_TABLES:
                conditions = self._visible_conditions(model) + self._filter_conditions(
                    model, filters
                )
                if since is not None:
                    conditions.append(model.ts >= since)
                if until is not None:
                    conditions.append(model.ts < until)
                stmt = (
                    select(model)
                    .where(*conditions)
                    .order_by(model.ts.desc(), model.id.desc())
                    .limit(pool)
                )
                for row in (await session.execute(stmt)).scalars():
                    records.append(self._record(row))
        records.sort(key=lambda r: (r.ts, str(r.id)), reverse=True)
        return records if pool is None else records[:pool]

    async def nearest_candidates(
        self, query_vec: list[float], pool: int, *, filters: SearchFilters | None = None
    ) -> list[tuple[EventRecord, float]]:
        """Exact database cosine ranking, with visibility and filters before LIMIT.

        Adding zero to PostgreSQL's distance order intentionally prevents approximate
        HNSW plans from silently returning fewer than the requested eligible pool.
        """
        validate_embedding(query_vec, DEFAULT_EMBEDDING_DIM)
        hits: list[tuple[EventRecord, float]] = []
        async with self._sessionmaker() as session:
            for model in _EVENT_TABLES:
                if self._is_sqlite:
                    blob = struct.pack(f"<{len(query_vec)}f", *query_vec)
                    distance = func.coalesce(
                        func.vec_distance_cosine(model.embedding, literal(blob, type_=LargeBinary)),
                        1.0,
                    )
                else:
                    distance = model.embedding.cosine_distance(query_vec)
                stmt = (
                    select(model, distance.label("distance"))
                    .where(
                        *self._visible_conditions(model), *self._filter_conditions(model, filters)
                    )
                    .order_by((distance + 0).asc(), model.id.asc())
                    .limit(pool)
                )
                for row in (await session.execute(stmt)).all():
                    hits.append(
                        (self._record(row[0]), float(row[1]) if row[1] is not None else 1.0)
                    )
        hits.sort(key=lambda h: (h[1], str(h[0].id)))
        return hits[:pool]

    async def history_chain(self, event_id: uuid.UUID) -> list[EventRecord]:
        """Connected supersession component, preserving every authorized branch."""
        records = await self._all_records()
        if event_id not in records:
            return []
        edges: dict[uuid.UUID, set[uuid.UUID]] = {}
        for rec in records.values():
            target = rec.supersedes
            if target in records and records[target].author == rec.author:
                edges.setdefault(rec.id, set()).add(target)
                edges.setdefault(target, set()).add(rec.id)
        pending = [event_id]
        seen: set[uuid.UUID] = set()
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(edges.get(current, set()) - seen)
        return sorted((records[i] for i in seen), key=lambda r: (r.created_at, str(r.id)))
