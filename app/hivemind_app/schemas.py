"""Pydantic v2 request/response schemas.

Request models carry **no identity fields**: authorship is server-enforced from
config, never accepted from a client. Any attempt to pass ``author`` — as a
top-level field or nested inside ``metadata`` — is rejected. Jira project/ticket
values are regex-validated; ``event_type`` and ``status`` are validated against
the controlled vocabularies.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from .config import (
    EVENT_TYPES,
    JIRA_PROJECT_RE,
    JIRA_TICKET_RE,
    STATUS_VALUES,
)

MAX_TITLE_LENGTH = 1000
MAX_BODY_LENGTH = 100_000
MAX_QUERY_LENGTH = 10_000
MAX_TAG_LENGTH = 100
MAX_TAGS = 100
MAX_METADATA_BYTES = 100_000
Tag = Annotated[str, Field(max_length=MAX_TAG_LENGTH)]

# ``tombstone`` is internal (produced only by delete); clients may not set it.
_CLIENT_EVENT_TYPES = frozenset(EVENT_TYPES - {"tombstone"})


def _reject_author_in_metadata(value: dict[str, Any]) -> dict[str, Any]:
    """Reject any ``author``-like key in a metadata mapping."""
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    for key in value:
        if key.lower() == "author":
            raise ValueError("author is derived from identity and must not appear in metadata")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("metadata must contain valid JSON values") from exc
    if len(encoded) > MAX_METADATA_BYTES:
        raise ValueError(f"metadata must not exceed {MAX_METADATA_BYTES} JSON bytes")
    return value


def _validate_event_type(value: str) -> str:
    if value not in _CLIENT_EVENT_TYPES:
        allowed = ", ".join(sorted(_CLIENT_EVENT_TYPES))
        raise ValueError(f"invalid event_type {value!r}; allowed: {allowed}")
    return value


def _validate_status(value: str) -> str:
    if value not in STATUS_VALUES:
        allowed = ", ".join(sorted(STATUS_VALUES))
        raise ValueError(f"invalid status {value!r}; allowed: {allowed}")
    return value


def _validate_jira_project(value: str) -> str:
    if not JIRA_PROJECT_RE.match(value):
        raise ValueError(f"invalid jira_project {value!r}; must match {JIRA_PROJECT_RE.pattern}")
    return value


def _validate_jira_ticket(value: str | None) -> str | None:
    if value is None:
        return None
    if not JIRA_TICKET_RE.match(value):
        raise ValueError(f"invalid jira_ticket {value!r}; must match {JIRA_TICKET_RE.pattern}")
    return value


class MemoryAddRequest(BaseModel):
    """Validated payload for creating a memory record.

    ``extra='forbid'`` guarantees an ``author`` (or any other stray field) is
    rejected outright rather than silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    body: str = Field(max_length=MAX_BODY_LENGTH)
    jira_project: str
    jira_ticket: str | None = None
    event_type: str = "update"
    status: str = "info"
    tags: list[Tag] = Field(default_factory=list, max_length=MAX_TAGS)
    metadata: dict[str, Any] = Field(default_factory=dict)
    ts: datetime | None = None

    _v_project = field_validator("jira_project")(_validate_jira_project)
    _v_ticket = field_validator("jira_ticket")(_validate_jira_ticket)
    _v_event = field_validator("event_type")(_validate_event_type)
    _v_status = field_validator("status")(_validate_status)
    _v_meta = field_validator("metadata")(_reject_author_in_metadata)


class MemoryUpdateRequest(BaseModel):
    """Validated payload for superseding an existing (own) record."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    body: str = Field(max_length=MAX_BODY_LENGTH)
    jira_project: str
    jira_ticket: str | None = None
    event_type: str = "update"
    status: str = "info"
    tags: list[Tag] = Field(default_factory=list, max_length=MAX_TAGS)
    metadata: dict[str, Any] = Field(default_factory=dict)

    _v_project = field_validator("jira_project")(_validate_jira_project)
    _v_ticket = field_validator("jira_ticket")(_validate_jira_ticket)
    _v_event = field_validator("event_type")(_validate_event_type)
    _v_status = field_validator("status")(_validate_status)
    _v_meta = field_validator("metadata")(_reject_author_in_metadata)


class SearchFilters(BaseModel):
    """Optional filters shared by list_recent and search."""

    model_config = ConfigDict(extra="forbid")

    author: str | None = None
    event_type: str | None = None
    status: str | None = None
    jira_project: str | None = None
    jira_ticket: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    tags: list[Tag] | None = Field(default=None, max_length=MAX_TAGS)


class EventOut(BaseModel):
    """Response model for a single visible memory record.

    This is an *output* shape and therefore includes identity fields (``id``,
    ``author``). It never appears on the request side.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author: str
    event_type: str
    status: str
    title: str
    body: str
    jira_project: str
    jira_ticket: str | None
    tags: list[str]
    metadata: dict[str, Any]
    supersedes: uuid.UUID | None
    deletes: uuid.UUID | None
    ts: datetime
    created_at: datetime
