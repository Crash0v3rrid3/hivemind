"""MemoryService — the application core.

Orchestrates validated requests into append-only records, computes the visible
state (two-table merge, hidden-id filtering, distance re-rank, pool over-fetch),
and enforces the authorship invariant: **local writes are always authored as
``OWNER_HANDLE`` from identity, never from client input**, and updates/deletes
touch only the owner's own records.

The merge / filter / re-rank primitives are pure, module-level functions so they
are unit-testable with fake hits and no database. The service depends on a small
async repository protocol, so a fake repository drives it in tests.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol

from .config import Identity
from .embeddings import Embedder, build_embedding_text, zero_vector
from .schemas import MAX_QUERY_LENGTH, MemoryAddRequest, MemoryUpdateRequest, SearchFilters
from .serialization import EventRecord, day_bucket

# Default over-fetch pool floor for both recent and search (SPEC §8).
POOL_FLOOR = 200
MAX_READ_LIMIT = 1000


class NotOwnRecordError(PermissionError):
    """Raised when trying to update/delete a record the owner didn't author."""


class RecordNotFoundError(LookupError):
    """Raised when an update/delete target id does not exist."""


# ---------------------------------------------------------------------------
# Repository protocol (async). A fake implementation drives the unit tests.
# ---------------------------------------------------------------------------


class RepositoryLike(Protocol):
    """The subset of the repository the service depends on."""

    async def insert_local(self, record: EventRecord) -> None: ...

    async def get_by_id(self, event_id: uuid.UUID) -> EventRecord | None: ...

    async def hidden_ids(self) -> set[uuid.UUID]: ...

    async def recent_candidates(
        self,
        pool: int | None,
        *,
        filters: SearchFilters | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[EventRecord]: ...

    async def nearest_candidates(
        self, query_vec: list[float], pool: int, *, filters: SearchFilters | None = None
    ) -> list[tuple[EventRecord, float]]: ...

    async def history_chain(self, event_id: uuid.UUID) -> list[EventRecord]: ...


# ---------------------------------------------------------------------------
# Pure helpers (no DB, no I/O) — the testable core of merge/filter/re-rank.
# ---------------------------------------------------------------------------


def matches_filters(record: EventRecord, filters: SearchFilters | None) -> bool:
    """Return True if ``record`` satisfies every set filter.

    ``tags`` matches when the record contains ALL requested tags (subset).
    ``date_from``/``date_to`` are inclusive bounds on ``ts`` (UTC-compared).
    """
    if filters is None:
        return True
    if filters.author is not None and record.author != filters.author:
        return False
    if filters.event_type is not None and record.event_type != filters.event_type:
        return False
    if filters.status is not None and record.status != filters.status:
        return False
    if filters.jira_project is not None and record.jira_project != filters.jira_project:
        return False
    if filters.jira_ticket is not None and record.jira_ticket != filters.jira_ticket:
        return False
    if filters.date_from is not None and _as_utc(record.ts) < _as_utc(filters.date_from):
        return False
    if filters.date_to is not None and _as_utc(record.ts) > _as_utc(filters.date_to):
        return False
    if filters.tags:
        rtags = set(record.tags)
        if not set(filters.tags).issubset(rtags):
            return False
    return True


def drop_hidden(records: list[EventRecord], hidden_ids: set[uuid.UUID]) -> list[EventRecord]:
    """Remove records that are hidden (superseded/deleted) or tombstones."""
    return [r for r in records if r.id not in hidden_ids and r.event_type != "tombstone"]


def rerank_by_distance(
    hits: list[tuple[EventRecord, float]],
) -> list[tuple[EventRecord, float]]:
    """Sort hits by ascending cosine distance (closest first), stably."""
    return sorted(hits, key=lambda h: h[1])


def dedupe_hits(
    hits: list[tuple[EventRecord, float]],
) -> list[tuple[EventRecord, float]]:
    """Drop duplicate record ids, keeping the smallest distance for each."""
    best: dict[uuid.UUID, tuple[EventRecord, float]] = {}
    for record, dist in hits:
        existing = best.get(record.id)
        if existing is None or dist < existing[1]:
            best[record.id] = (record, dist)
    return list(best.values())


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


class MemoryService:
    """Coordinates writes/reads while enforcing the identity invariant."""

    def __init__(
        self,
        repository: RepositoryLike,
        embedder: Embedder,
        identity: Identity,
        clock: Callable[[], datetime] | None = None,
        embedding_model: str | None = None,
    ) -> None:
        self._embedding_model = embedding_model
        self._embed_lock = asyncio.Lock()
        self._repo = repository
        self._embedder = embedder
        self._identity = identity
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def owner_handle(self) -> str:
        return self._identity.owner_handle

    @property
    def roster(self) -> tuple[str, ...]:
        return self._identity.roster

    # -- writes ---------------------------------------------------------------

    async def add(self, req: MemoryAddRequest) -> EventRecord:
        """Create a new record authored as the owner."""
        now = self._clock()
        ts = _as_utc(req.ts) if req.ts is not None else now
        record = EventRecord(
            id=uuid.uuid4(),
            embedding_model=self._embedding_model,
            author=self.owner_handle,  # identity, never client input
            event_type=req.event_type,
            status=req.status,
            title=req.title,
            body=req.body,
            jira_project=req.jira_project,
            jira_ticket=req.jira_ticket,
            tags=list(req.tags),
            metadata=dict(req.metadata),
            supersedes=None,
            deletes=None,
            ts=ts,
            created_at=now,
            embedding=await self._embed(build_embedding_text(req.title, req.body)),
        )
        await self._repo.insert_local(record)
        return record

    async def update(self, req: MemoryUpdateRequest) -> EventRecord:
        """Supersede an existing OWN record with a new one (new id)."""
        old = await self._repo.get_by_id(req.id)
        if old is None:
            raise RecordNotFoundError(f"no record with id {req.id}")
        if old.author != self.owner_handle:
            raise NotOwnRecordError(f"cannot update record authored by {old.author!r}")
        now = self._clock()
        record = EventRecord(
            id=uuid.uuid4(),
            embedding_model=self._embedding_model,
            author=self.owner_handle,
            event_type=req.event_type,
            status=req.status,
            title=req.title,
            body=req.body,
            jira_project=req.jira_project,
            jira_ticket=req.jira_ticket,
            tags=list(req.tags),
            metadata=dict(req.metadata),
            supersedes=req.id,
            deletes=None,
            ts=now,
            created_at=now,
            embedding=await self._embed(build_embedding_text(req.title, req.body)),
        )
        await self._repo.insert_local(record)
        return record

    async def delete(self, event_id: uuid.UUID) -> EventRecord:
        """Emit a tombstone for an existing OWN record."""
        old = await self._repo.get_by_id(event_id)
        if old is None:
            raise RecordNotFoundError(f"no record with id {event_id}")
        if old.author != self.owner_handle:
            raise NotOwnRecordError(f"cannot delete record authored by {old.author!r}")
        now = self._clock()
        record = EventRecord(
            id=uuid.uuid4(),
            embedding_model=self._embedding_model,
            author=self.owner_handle,
            event_type="tombstone",
            status="info",
            title="",
            body="",
            jira_project=old.jira_project,
            jira_ticket=old.jira_ticket,
            tags=[],
            metadata={},
            supersedes=None,
            deletes=event_id,
            ts=now,
            created_at=now,
            embedding=zero_vector(self._embedder.dim),
        )
        await self._repo.insert_local(record)
        return record

    # -- reads ----------------------------------------------------------------

    async def get(self, event_id: uuid.UUID) -> EventRecord | None:
        """Return a record by id (raw; may be hidden)."""
        return await self._repo.get_by_id(event_id)

    async def list_recent(
        self, limit: int = 50, filters: SearchFilters | None = None
    ) -> list[EventRecord]:
        """Most-recent visible records across both tables, filtered."""
        _validate_limit(limit, "limit")
        hidden = await self._repo.hidden_ids()
        pool = max(limit, POOL_FLOOR)
        candidates = await self._repo.recent_candidates(pool, filters=filters)
        visible = drop_hidden(candidates, hidden)
        filtered = [r for r in visible if matches_filters(r, filters)]
        filtered.sort(key=lambda r: _as_utc(r.ts), reverse=True)
        return filtered[:limit]

    async def search(
        self,
        query: str,
        top_k: int = 20,
        filters: SearchFilters | None = None,
    ) -> list[EventRecord]:
        """Semantic search across both tables with pool over-fetch + re-rank."""
        _validate_limit(top_k, "top_k")
        if not isinstance(query, str) or not 1 <= len(query) <= MAX_QUERY_LENGTH:
            raise ValueError(f"query must contain 1 to {MAX_QUERY_LENGTH} characters")
        query_vec = await self._embed(query)
        pool = max(top_k, POOL_FLOOR)
        hits = await self._repo.nearest_candidates(query_vec, pool, filters=filters)
        hits = dedupe_hits(hits)
        hidden = await self._repo.hidden_ids()
        visible = [(r, d) for (r, d) in hits if r.id not in hidden and r.event_type != "tombstone"]
        visible = [(r, d) for (r, d) in visible if matches_filters(r, filters)]
        ranked = rerank_by_distance(visible)
        return [r for (r, _d) in ranked[:top_k]]

    async def history(self, event_id: uuid.UUID) -> list[EventRecord]:
        """Return the supersede chain for an id, ordered by created_at."""
        return await self._repo.history_chain(event_id)

    async def daily_summary(
        self, day: str | None = None, author: str | None = None
    ) -> dict[str, object]:
        """Per-author, per-event-type counts of visible records for a UTC day."""
        target_day = day or day_bucket(self._clock())
        parsed_day = date.fromisoformat(target_day)
        if parsed_day.isoformat() != target_day:
            raise ValueError("day must use YYYY-MM-DD format")
        since = datetime.combine(parsed_day, time.min, tzinfo=UTC)
        hidden = await self._repo.hidden_ids()
        candidates = await self._repo.recent_candidates(
            None,
            filters=SearchFilters(author=author),
            since=since,
            until=since + timedelta(days=1),
        )
        visible = drop_hidden(candidates, hidden)

        authors: dict[str, dict[str, int]] = {}
        total = 0
        for record in visible:
            if day_bucket(record.ts) != target_day:
                continue
            if author is not None and record.author != author:
                continue
            by_type = authors.setdefault(record.author, {})
            by_type[record.event_type] = by_type.get(record.event_type, 0) + 1
            total += 1

        return {"day": target_day, "authors": authors, "total": total}

    # -- internals ------------------------------------------------------------

    async def _embed(self, text: str) -> list[float]:
        # Serialize lazy model initialization/inference while leaving the event loop free.
        async with self._embed_lock:
            vec = await asyncio.to_thread(self._embedder.embed, text)
        if len(vec) != self._embedder.dim:
            raise ValueError(f"embedder returned dim {len(vec)}, expected {self._embedder.dim}")
        return vec


def _validate_limit(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_READ_LIMIT:
        raise ValueError(f"{name} must be an integer between 1 and {MAX_READ_LIMIT}")
