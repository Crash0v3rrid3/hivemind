"""SQLAlchemy 2.0 mapped models: local_events, team_events, ingested_files.

``local_events`` holds records this node's owner authored (authoritative).
``team_events`` is the ingested cache of other members' records; it has the
identical column shape (the SQL ``CREATE TABLE team_events (LIKE local_events
INCLUDING ALL)`` is modelled here as a shared abstract mixin so both mapped
classes carry the exact same columns).

Only ``local_events`` is ever exported, so ``exported_at`` is meaningful there;
it exists on both tables to keep the shapes identical (as ``LIKE ... INCLUDING
ALL`` would).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    DateTime,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from .config import DEFAULT_EMBEDDING_DIM
from .db import Base

# Embedding dimension is fixed at the schema level. The runtime embedder must
# agree with this; the invariant is asserted where vectors are produced.
EMBEDDING_DIM = DEFAULT_EMBEDDING_DIM


class _EventColumns:
    """The shared column set for both event tables.

    Declared as a mixin so ``local_events`` and ``team_events`` are guaranteed
    identical in shape, mirroring ``CREATE TABLE team_events (LIKE local_events
    INCLUDING ALL)``.
    """

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="info")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    jira_project: Mapped[str] = mapped_column(Text, nullable=False)
    jira_ticket: Mapped[str | None] = mapped_column(Text, nullable=True)

    @declared_attr
    def tags(cls) -> Mapped[list[str]]:
        return mapped_column(ARRAY(String), nullable=False, server_default="{}")

    @declared_attr
    def event_metadata(cls) -> Mapped[dict[str, Any]]:
        # Column name in the DB is ``metadata`` (reserved on the mapper, so the
        # Python attribute is ``event_metadata`` mapped to column ``metadata``).
        return mapped_column("metadata", JSONB, nullable=False, server_default="{}")

    supersedes: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    deletes: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    @declared_attr
    def embedding(cls) -> Mapped[list[float]]:
        return mapped_column(Vector(EMBEDDING_DIM), nullable=False)

    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LocalEvent(_EventColumns, Base):
    """Records authored by this node's owner (authoritative, exportable).

    The HNSW vector index is created separately via raw ``CREATE INDEX IF NOT
    EXISTS`` in the idempotent schema init (see ``repository.init_schema``), so
    it is intentionally absent here.
    """

    __tablename__ = "local_events"

    __table_args__ = (Index("ix_local_events_ts", "ts"),)


class TeamEvent(_EventColumns, Base):
    """Records ingested from other members (rebuildable cache).

    HNSW vector index created via raw SQL in schema init (see LocalEvent).
    """

    __tablename__ = "team_events"

    __table_args__ = (
        Index("ix_team_events_ts", "ts"),
        Index("ix_team_events_author", "author"),
    )


class IngestedFile(Base):
    """Bookkeeping of which NDJSON files have already been imported."""

    __tablename__ = "ingested_files"

    path: Mapped[str] = mapped_column(Text, primary_key=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
