"""Append-only semantics: supersede, tombstone, hidden_ids, history (SPEC §6, §14).

Driven through :class:`MemoryService` over the in-memory ``FakeRepository`` so
no database is required.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from hivemind_app.config import Identity
from hivemind_app.schemas import MemoryAddRequest, MemoryUpdateRequest
from hivemind_app.service import (
    MemoryService,
    NotOwnRecordError,
    RecordNotFoundError,
)


def _service(fake_repo, fake_embedder, identity, clock) -> MemoryService:  # type: ignore[no-untyped-def]
    return MemoryService(fake_repo, fake_embedder, identity, clock=clock)


def _add_req(**overrides: object) -> MemoryAddRequest:
    base: dict[str, Any] = {"title": "t", "body": "b", "jira_project": "BETEL"}
    base.update(overrides)
    return MemoryAddRequest(**base)


# ---------------------------------------------------------------------------
# Create.
# ---------------------------------------------------------------------------


async def test_add_creates_visible_record(fake_repo, fake_embedder, identity, clock) -> None:  # type: ignore[no-untyped-def]
    svc = _service(fake_repo, fake_embedder, identity, clock)
    rec = await svc.add(_add_req(title="hello"))
    assert rec.supersedes is None
    assert rec.deletes is None
    assert rec.author == "alice"
    assert rec.event_type == "update"
    assert len(fake_repo.local) == 1


async def test_add_authored_as_owner_only(fake_repo, fake_embedder, clock) -> None:  # type: ignore[no-untyped-def]
    svc = MemoryService(fake_repo, fake_embedder, Identity(owner_handle="zoe"), clock=clock)
    rec = await svc.add(_add_req())
    assert rec.author == "zoe"


# ---------------------------------------------------------------------------
# Update -> supersede.
# ---------------------------------------------------------------------------


async def test_update_sets_supersedes(fake_repo, fake_embedder, identity, clock) -> None:  # type: ignore[no-untyped-def]
    svc = _service(fake_repo, fake_embedder, identity, clock)
    original = await svc.add(_add_req(title="v1"))
    updated = await svc.update(
        MemoryUpdateRequest(id=original.id, title="v2", body="b", jira_project="BETEL")
    )
    assert updated.id != original.id  # new row, new id
    assert updated.supersedes == original.id
    assert updated.author == "alice"


async def test_update_missing_target_raises(fake_repo, fake_embedder, identity, clock) -> None:  # type: ignore[no-untyped-def]
    svc = _service(fake_repo, fake_embedder, identity, clock)
    with pytest.raises(RecordNotFoundError):
        await svc.update(
            MemoryUpdateRequest(id=uuid.uuid4(), title="t", body="b", jira_project="BETEL")
        )


async def test_cannot_update_other_authors_record(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock, make_record
) -> None:
    # A record ingested from bob lives in team_events.
    foreign = make_record(author="bob")
    await fake_repo.import_records([foreign])
    svc = _service(fake_repo, fake_embedder, identity, clock)
    with pytest.raises(NotOwnRecordError):
        await svc.update(
            MemoryUpdateRequest(id=foreign.id, title="t", body="b", jira_project="BETEL")
        )
    # No new local record was written.
    assert fake_repo.local == []


# ---------------------------------------------------------------------------
# Delete -> tombstone.
# ---------------------------------------------------------------------------


async def test_delete_emits_tombstone(fake_repo, fake_embedder, identity, clock) -> None:  # type: ignore[no-untyped-def]
    svc = _service(fake_repo, fake_embedder, identity, clock)
    original = await svc.add(_add_req(title="doomed"))
    tomb = await svc.delete(original.id)
    assert tomb.event_type == "tombstone"
    assert tomb.deletes == original.id
    assert tomb.supersedes is None
    assert tomb.title == ""
    assert tomb.body == ""
    assert all(x == 0.0 for x in tomb.embedding)  # zero-vector embedding
    assert len(tomb.embedding) == fake_embedder.dim


async def test_delete_missing_target_raises(fake_repo, fake_embedder, identity, clock) -> None:  # type: ignore[no-untyped-def]
    svc = _service(fake_repo, fake_embedder, identity, clock)
    with pytest.raises(RecordNotFoundError):
        await svc.delete(uuid.uuid4())


async def test_cannot_delete_other_authors_record(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock, make_record
) -> None:
    foreign = make_record(author="bob")
    await fake_repo.import_records([foreign])
    svc = _service(fake_repo, fake_embedder, identity, clock)
    with pytest.raises(NotOwnRecordError):
        await svc.delete(foreign.id)
    assert fake_repo.local == []


# ---------------------------------------------------------------------------
# hidden_ids computation.
# ---------------------------------------------------------------------------


async def test_hidden_ids_includes_superseded(fake_repo, fake_embedder, identity, clock) -> None:  # type: ignore[no-untyped-def]
    svc = _service(fake_repo, fake_embedder, identity, clock)
    original = await svc.add(_add_req(title="v1"))
    await svc.update(
        MemoryUpdateRequest(id=original.id, title="v2", body="b", jira_project="BETEL")
    )
    hidden = await fake_repo.hidden_ids()
    assert original.id in hidden


async def test_hidden_ids_includes_deleted(fake_repo, fake_embedder, identity, clock) -> None:  # type: ignore[no-untyped-def]
    svc = _service(fake_repo, fake_embedder, identity, clock)
    original = await svc.add(_add_req())
    await svc.delete(original.id)
    hidden = await fake_repo.hidden_ids()
    assert original.id in hidden


async def test_superseded_record_hidden_from_list(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    original = await svc.add(_add_req(title="v1"))
    updated = await svc.update(
        MemoryUpdateRequest(id=original.id, title="v2", body="b", jira_project="BETEL")
    )
    visible = await svc.list_recent(limit=10)
    ids = {r.id for r in visible}
    assert original.id not in ids  # superseded -> hidden
    assert updated.id in ids  # the new revision is visible


async def test_deleted_record_and_tombstone_both_hidden(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    original = await svc.add(_add_req())
    await svc.delete(original.id)
    visible = await svc.list_recent(limit=10)
    assert visible == []  # original hidden; tombstone excluded


# ---------------------------------------------------------------------------
# History chain ordering.
# ---------------------------------------------------------------------------


async def test_history_chain_ordered_by_created_at(  # type: ignore[no-untyped-def]
    fake_repo, fake_embedder, identity, clock
) -> None:
    svc = _service(fake_repo, fake_embedder, identity, clock)
    v1 = await svc.add(_add_req(title="v1"))
    v2 = await svc.update(MemoryUpdateRequest(id=v1.id, title="v2", body="b", jira_project="BETEL"))
    v3 = await svc.update(MemoryUpdateRequest(id=v2.id, title="v3", body="b", jira_project="BETEL"))
    # History resolvable from ANY id in the chain, always oldest-first.
    for anchor in (v1.id, v2.id, v3.id):
        chain = await svc.history(anchor)
        assert [r.id for r in chain] == [v1.id, v2.id, v3.id]
        assert [r.title for r in chain] == ["v1", "v2", "v3"]


async def test_history_unknown_id_empty(fake_repo, fake_embedder, identity, clock) -> None:  # type: ignore[no-untyped-def]
    svc = _service(fake_repo, fake_embedder, identity, clock)
    assert await svc.history(uuid.uuid4()) == []
