"""Tests for Pydantic request/response schemas (SPEC §9, §14).

Focus: author-rejection (top-level and nested in metadata), Jira regex
validation, event_type / status enum enforcement, and that the request models
carry no identity fields.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from hivemind_app.schemas import (
    EventOut,
    MemoryAddRequest,
    MemoryUpdateRequest,
    SearchFilters,
)


def _valid_add(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "title": "t",
        "body": "b",
        "jira_project": "BETEL",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_add_minimal_valid() -> None:
    req = MemoryAddRequest(**_valid_add())
    assert req.title == "t"
    assert req.event_type == "update"
    assert req.status == "info"
    assert req.tags == []
    assert req.metadata == {}
    assert req.jira_ticket is None
    assert req.ts is None


def test_add_full_valid() -> None:
    req = MemoryAddRequest(
        **_valid_add(
            jira_ticket="BETEL-42",
            event_type="decision",
            status="open",
            tags=["a", "b"],
            metadata={"minutes_spent": 10},
            ts=datetime(2026, 8, 19, tzinfo=UTC),
        )
    )
    assert req.jira_ticket == "BETEL-42"
    assert req.event_type == "decision"
    assert req.status == "open"


# ---------------------------------------------------------------------------
# Author rejection — the core identity invariant.
# ---------------------------------------------------------------------------


def test_add_rejects_top_level_author() -> None:
    with pytest.raises(ValidationError):
        MemoryAddRequest(**_valid_add(author="mallory"))


def test_add_rejects_author_in_metadata() -> None:
    with pytest.raises(ValidationError):
        MemoryAddRequest(**_valid_add(metadata={"author": "mallory"}))


def test_add_rejects_author_in_metadata_case_insensitive() -> None:
    with pytest.raises(ValidationError):
        MemoryAddRequest(**_valid_add(metadata={"Author": "mallory"}))
    with pytest.raises(ValidationError):
        MemoryAddRequest(**_valid_add(metadata={"AUTHOR": "mallory"}))


def test_update_rejects_author_in_metadata() -> None:
    with pytest.raises(ValidationError):
        MemoryUpdateRequest(
            id=uuid.uuid4(),
            title="t",
            body="b",
            jira_project="BETEL",
            metadata={"author": "x"},
        )


def test_add_rejects_arbitrary_extra_field() -> None:
    with pytest.raises(ValidationError):
        MemoryAddRequest(**_valid_add(surprise="value"))


# ---------------------------------------------------------------------------
# Jira validation.
# ---------------------------------------------------------------------------


def test_add_rejects_lowercase_project() -> None:
    with pytest.raises(ValidationError):
        MemoryAddRequest(**_valid_add(jira_project="betel"))


def test_add_rejects_bad_ticket() -> None:
    with pytest.raises(ValidationError):
        MemoryAddRequest(**_valid_add(jira_ticket="betel-1"))


def test_add_accepts_none_ticket() -> None:
    req = MemoryAddRequest(**_valid_add(jira_ticket=None))
    assert req.jira_ticket is None


# ---------------------------------------------------------------------------
# Enum enforcement.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event_type",
    [
        "update",
        "decision",
        "blocker",
        "dependency",
        "handoff",
        "research",
        "context",
        "note",
        "friction",
        "ai_session",
    ],
)
def test_add_accepts_all_client_event_types(event_type: str) -> None:
    req = MemoryAddRequest(**_valid_add(event_type=event_type))
    assert req.event_type == event_type


def test_add_rejects_tombstone_event_type() -> None:
    # tombstone is internal; clients may not set it.
    with pytest.raises(ValidationError):
        MemoryAddRequest(**_valid_add(event_type="tombstone"))


def test_add_rejects_unknown_event_type() -> None:
    with pytest.raises(ValidationError):
        MemoryAddRequest(**_valid_add(event_type="nonsense"))


@pytest.mark.parametrize("status", ["open", "in_progress", "resolved", "done", "info"])
def test_add_accepts_all_statuses(status: str) -> None:
    req = MemoryAddRequest(**_valid_add(status=status))
    assert req.status == status


def test_add_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        MemoryAddRequest(**_valid_add(status="pending"))


def test_add_rejects_empty_title() -> None:
    with pytest.raises(ValidationError):
        MemoryAddRequest(**_valid_add(title=""))


# ---------------------------------------------------------------------------
# Update request.
# ---------------------------------------------------------------------------


def test_update_requires_id() -> None:
    with pytest.raises(ValidationError):
        MemoryUpdateRequest(title="t", body="b", jira_project="BETEL")  # type: ignore[call-arg]


def test_update_valid() -> None:
    rid = uuid.uuid4()
    req = MemoryUpdateRequest(id=rid, title="t", body="b", jira_project="BETEL")
    assert req.id == rid


def test_update_rejects_author_field() -> None:
    with pytest.raises(ValidationError):
        MemoryUpdateRequest(
            id=uuid.uuid4(),
            title="t",
            body="b",
            jira_project="BETEL",
            author="x",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# SearchFilters — no author write path, but author is a *read* filter.
# ---------------------------------------------------------------------------


def test_search_filters_defaults_all_none() -> None:
    f = SearchFilters()
    assert f.author is None
    assert f.event_type is None
    assert f.tags is None


def test_search_filters_rejects_extra() -> None:
    with pytest.raises(ValidationError):
        SearchFilters(nope=1)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# EventOut — the response shape (identity fields present).
# ---------------------------------------------------------------------------


def test_event_out_from_attributes(make_record) -> None:  # type: ignore[no-untyped-def]
    record = make_record(jira_ticket="BETEL-7", tags=["x"])
    out = EventOut.model_validate(record)
    assert out.id == record.id
    assert out.author == record.author
    assert out.jira_ticket == "BETEL-7"
    dumped = out.model_dump(mode="json")
    assert "embedding" not in dumped  # never leak vectors to clients
    assert dumped["author"] == record.author
