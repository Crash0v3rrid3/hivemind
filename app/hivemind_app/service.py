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

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from .config import Identity
from .embeddings import Embedder, build_embedding_text, zero_vector
from .schemas import MemoryAddRequest, MemoryUpdateRequest, SearchFilters
from .serialization import EventRecord, day_bucket

# Default over-fetch pool floor for both recent and search (SPEC §8).
POOL_FLOOR = 200


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

    async def recent_candidates(self, pool: int) -> list[EventRecord]: ...

    async def nearest_candidates(
        self, query_vec: list[float], pool: int
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
    ) -> None:
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
            embedding=self._embed(req.title, req.body),
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
            embedding=self._embed(req.title, req.body),
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
        hidden = await self._repo.hidden_ids()
        pool = max(limit, POOL_FLOOR)
        candidates = await self._repo.recent_candidates(pool)
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
        query_vec = self._embedder.embed(query)
        pool = max(top_k, POOL_FLOOR)
        hits = await self._repo.nearest_candidates(query_vec, pool)
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
        hidden = await self._repo.hidden_ids()
        # Fetch a large pool; a single day of team memory fits comfortably.
        candidates = await self._repo.recent_candidates(max(POOL_FLOOR, 10000))
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

    def _embed(self, title: str, body: str) -> list[float]:
        vec = self._embedder.embed(build_embedding_text(title, body))
        if len(vec) != self._embedder.dim:
            raise ValueError(f"embedder returned dim {len(vec)}, expected {self._embedder.dim}")
        return vec
