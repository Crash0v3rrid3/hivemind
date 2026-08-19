"""Service merge / search: pool over-fetch, two-table union, hidden filtering,
distance re-rank, and filter application (SPEC §8, §14).

The pure helpers are tested directly; the end-to-end paths are driven through
:class:`MemoryService` over the in-memory ``FakeRepository``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from hivemind_app.schemas import MemoryAddRequest, SearchFilters
from hivemind_app.service import (
    POOL_FLOOR,
    MemoryService,
    dedupe_hits,
    drop_hidden,
    matches_filters,
    rerank_by_distance,
)


def _service(fake_repo, fake_embedder, identity, clock) -> MemoryService:  # type: ignore[no-untyped-def]
    return MemoryService(fake_repo, fake_embedder, identity, clock=clock)


# ---------------------------------------------------------------------------
# Pure helper: matches_filters.
# ---------------------------------------------------------------------------


def test_matches_filters_none_passes(make_record) -> None:  # type: ignore[no-untyped-def]
    assert matches_filters(make_record(), None) is True


def test_matches_filters_author(make_record) -> None:  # type: ignore[no-untyped-def]
    rec = make_record(author="bob")
    assert matches_filters(rec, SearchFilters(author="bob")) is True
    assert matches_filters(rec, SearchFilters(author="alice")) is False


def test_matches_filters_event_type_and_status(make_record) -> None:  # type: ignore[no-untyped-def]
    rec = make_record(event_type="blocker", status="open")
    assert matches_filters(rec, SearchFilters(event_type="blocker")) is True
    assert matches_filters(rec, SearchFilters(event_type="decision")) is False
    assert matches_filters(rec, SearchFilters(status="open")) is True
    assert matches_filters(rec, SearchFilters(status="done")) is False


def test_matches_filters_jira(make_record) -> None:  # type: ignore[no-untyped-def]
    rec = make_record(jira_project="BETEL", jira_ticket="BETEL-1")
    assert matches_filters(rec, SearchFilters(jira_project="BETEL")) is True
    assert matches_filters(rec, SearchFilters(jira_project="OTHER")) is False
    assert matches_filters(rec, SearchFilters(jira_ticket="BETEL-1")) is True
    assert matches_filters(rec, SearchFilters(jira_ticket="BETEL-2")) is False


def test_matches_filters_tags_subset(make_record) -> None:  # type: ignore[no-untyped-def]
    rec = make_record(tags=["ci", "flaky", "python"])
    assert matches_filters(rec, SearchFilters(tags=["ci"])) is True
    assert matches_filters(rec, SearchFilters(tags=["ci", "flaky"])) is True
    assert matches_filters(rec, SearchFilters(tags=["ci", "missing"])) is False


def test_matches_filters_date_bounds(make_record) -> None:  # type: ignore[no-untyped-def]
    rec = make_record(ts=datetime(2026, 8, 19, 12, 0, tzinfo=UTC))
    assert matches_filters(rec, SearchFilters(date_from=datetime(2026, 8, 19, tzinfo=UTC))) is True
    assert matches_filters(rec, SearchFilters(date_from=datetime(2026, 8, 20, tzinfo=UTC))) is False
    assert matches_filters(rec, SearchFilters(date_to=datetime(2026, 8, 20, tzinfo=UTC))) is True
    assert matches_filters(rec, SearchFilters(date_to=datetime(2026, 8, 18, tzinfo=UTC))) is False


def test_matches_filters_naive_date_bound(make_record) -> None:  # type: ignore[no-untyped-def]
    # A naive bound is treated as UTC, matching an aware ts.
    rec = make_record(ts=datetime(2026, 8, 19, 12, 0, tzinfo=UTC))
    assert matches_filters(rec, SearchFilters(date_from=datetime(2026, 8, 19))) is True


# ---------------------------------------------------------------------------
# Pure helper: drop_hidden.
# ---------------------------------------------------------------------------


def test_drop_hidden_removes_hidden_and_tombstones(make_record) -> None:  # type: ignore[no-untyped-def]
    keep = make_record()
    hidden = make_record()
    tomb = make_record(event_type="tombstone")
    result = drop_hidden([keep, hidden, tomb], {hidden.id})
    assert result == [keep]


# ---------------------------------------------------------------------------
# Pure helper: rerank_by_distance / dedupe_hits.
# ---------------------------------------------------------------------------


def test_rerank_by_distance(make_record) -> None:  # type: ignore[no-untyped-def]
    a, b, c = make_record(), make_record(), make_record()
    hits = [(a, 0.9), (b, 0.1), (c, 0.5)]
    ranked = rerank_by_distance(hits)
    assert [r.id for r, _ in ranked] == [b.id, c.id, a.id]


def test_dedupe_hits_keeps_smallest_distance(make_record) -> None:  # type: ignore[no-untyped-def]
    rec = make_record()
    hits = [(rec, 0.8), (rec, 0.2), (rec, 0.5)]
    deduped = dedupe_hits(hits)
    assert len(deduped) == 1
    assert deduped[0][1] == 0.2


# ---------------------------------------------------------------------------
# list_recent: two-table union, pool over-fetch, ordering, filters.
# ---------------------------------------------------------------------------


async def test_list_recent_unions_both_tables(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock, make_record
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    local_rec = make_record(author="alice", ts=datetime(2026, 8, 19, 9, tzinfo=UTC))
    team_rec = make_record(author="bob", ts=datetime(2026, 8, 19, 8, tzinfo=UTC))
    await fake_repo.insert_local(local_rec)
    await fake_repo.import_records([team_rec])
    result = await svc.list_recent(limit=10)
    assert {r.id for r in result} == {local_rec.id, team_rec.id}


async def test_list_recent_orders_by_ts_desc(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock, make_record
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    older = make_record(ts=datetime(2026, 8, 19, 8, tzinfo=UTC))
    newer = make_record(ts=datetime(2026, 8, 19, 12, tzinfo=UTC))
    await fake_repo.insert_local(older)
    await fake_repo.insert_local(newer)
    result = await svc.list_recent(limit=10)
    assert [r.id for r in result] == [newer.id, older.id]


async def test_list_recent_pool_over_fetch(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    await svc.list_recent(limit=5)
    assert fake_repo.last_recent_pool == POOL_FLOOR  # max(5, 200)


async def test_list_recent_pool_respects_large_limit(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    await svc.list_recent(limit=500)
    assert fake_repo.last_recent_pool == 500  # max(500, 200)


async def test_list_recent_applies_limit(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock, make_record
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    for i in range(5):
        await fake_repo.insert_local(make_record(ts=datetime(2026, 8, 19, i + 1, tzinfo=UTC)))
    result = await svc.list_recent(limit=2)
    assert len(result) == 2


async def test_list_recent_applies_filters(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock, make_record
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    await fake_repo.insert_local(make_record(event_type="blocker"))
    await fake_repo.insert_local(make_record(event_type="decision"))
    result = await svc.list_recent(limit=10, filters=SearchFilters(event_type="blocker"))
    assert len(result) == 1
    assert result[0].event_type == "blocker"


async def test_list_recent_excludes_hidden(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock, make_record
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    old = make_record()
    new = make_record(supersedes=old.id)
    await fake_repo.insert_local(old)
    await fake_repo.insert_local(new)
    result = await svc.list_recent(limit=10)
    ids = {r.id for r in result}
    assert old.id not in ids
    assert new.id in ids


# ---------------------------------------------------------------------------
# search: pool over-fetch, dedupe, hidden filtering, re-rank, filters.
# ---------------------------------------------------------------------------


async def test_search_pool_over_fetch(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    await svc.search("query", top_k=10)
    assert fake_repo.last_nearest_pool == POOL_FLOOR


async def test_search_reranks_by_distance(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock, make_record
) -> None:
    # Force a specific, out-of-order candidate set to prove the service re-ranks.
    svc = _service(fake_repo, fake_embedder, identity, clock)
    near = make_record(title="near")
    mid = make_record(title="mid")
    far = make_record(title="far")
    fake_repo.nearest_override = [(far, 0.9), (near, 0.1), (mid, 0.5)]
    result = await svc.search("q", top_k=10)
    assert [r.id for r in result] == [near.id, mid.id, far.id]


async def test_search_drops_hidden_and_tombstones(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock, make_record
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    visible = make_record(title="v")
    hidden = make_record(title="h")
    tomb = make_record(title="t", event_type="tombstone")
    # Register a superseding record so `hidden` is in hidden_ids.
    superseder = make_record(supersedes=hidden.id)
    await fake_repo.insert_local(superseder)
    fake_repo.nearest_override = [(visible, 0.1), (hidden, 0.2), (tomb, 0.3)]
    result = await svc.search("q", top_k=10)
    ids = {r.id for r in result}
    assert visible.id in ids
    assert hidden.id not in ids
    assert tomb.id not in ids


async def test_search_applies_filters(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock, make_record
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    a = make_record(author="alice")
    b = make_record(author="bob")
    fake_repo.nearest_override = [(a, 0.1), (b, 0.2)]
    result = await svc.search("q", top_k=10, filters=SearchFilters(author="bob"))
    assert [r.id for r in result] == [b.id]


async def test_search_truncates_to_top_k(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock, make_record
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    hits = [(make_record(title=f"r{i}"), i / 100.0) for i in range(5)]
    fake_repo.nearest_override = hits
    result = await svc.search("q", top_k=2)
    assert len(result) == 2


async def test_search_dedupes_same_id_across_tables(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock, make_record
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    rec = make_record()
    fake_repo.nearest_override = [(rec, 0.5), (rec, 0.2)]
    result = await svc.search("q", top_k=10)
    assert [r.id for r in result] == [rec.id]


# ---------------------------------------------------------------------------
# daily_summary.
# ---------------------------------------------------------------------------


async def test_daily_summary_counts_by_author_and_type(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock, make_record
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    day = datetime(2026, 8, 19, 10, tzinfo=UTC)
    await fake_repo.insert_local(make_record(author="alice", event_type="update", ts=day))
    await fake_repo.insert_local(make_record(author="alice", event_type="update", ts=day))
    await fake_repo.import_records([make_record(author="bob", event_type="blocker", ts=day)])
    summary = await svc.daily_summary(day="2026-08-19")
    assert summary["day"] == "2026-08-19"
    assert summary["total"] == 3
    assert summary["authors"]["alice"]["update"] == 2
    assert summary["authors"]["bob"]["blocker"] == 1


async def test_daily_summary_filters_by_day(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock, make_record
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    await fake_repo.insert_local(make_record(ts=datetime(2026, 8, 19, 10, tzinfo=UTC)))
    await fake_repo.insert_local(make_record(ts=datetime(2026, 8, 20, 10, tzinfo=UTC)))
    summary = await svc.daily_summary(day="2026-08-19")
    assert summary["total"] == 1


async def test_daily_summary_filters_by_author(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock, make_record
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    day = datetime(2026, 8, 19, 10, tzinfo=UTC)
    await fake_repo.insert_local(make_record(author="alice", ts=day))
    await fake_repo.import_records([make_record(author="bob", ts=day)])
    summary = await svc.daily_summary(day="2026-08-19", author="bob")
    assert summary["total"] == 1
    assert set(summary["authors"].keys()) == {"bob"}


# ---------------------------------------------------------------------------
# Owner / roster passthrough.
# ---------------------------------------------------------------------------


async def test_service_exposes_owner_and_roster(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    assert svc.owner_handle == "alice"
    assert svc.roster == ("alice", "bob", "cara")


async def test_get_returns_record_or_none(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock, make_record
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    rec = make_record()
    await fake_repo.insert_local(rec)
    assert (await svc.get(rec.id)).id == rec.id
    assert await svc.get(uuid.uuid4()) is None


def _add_req(**overrides: object) -> MemoryAddRequest:
    base: dict[str, object] = {"title": "t", "body": "b", "jira_project": "BETEL"}
    base.update(overrides)
    return MemoryAddRequest(**base)


async def test_add_honors_explicit_ts(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    ts = datetime(2025, 1, 1, tzinfo=UTC)
    rec = await svc.add(_add_req(ts=ts))
    assert rec.ts == ts


async def test_add_defaults_ts_to_clock(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    rec = await svc.add(_add_req())
    # Clock starts at 2026-08-19T10:00:00Z.
    assert rec.ts == datetime(2026, 8, 19, 10, 0, 0, tzinfo=UTC)
