"""SQLAlchemy 2.0 mapped models: local_events, team_events, ingested_files.

``local_events`` holds records this node's owner authored (authoritative);
``team_events`` is the ingested cache of other members' records with the identical
column shape (shared via the ``_EventColumns`` mixin). Only ``local_events`` is
exported, so ``exported_at`` is meaningful there; it exists on both to keep shapes
identical.

Column types are cross-dialect so the same schema works on Postgres and SQLite:
- ``Embedding`` -> pgvector ``Vector`` on Postgres, float32 ``BLOB`` on SQLite
  (sqlite-vec reads that BLOB layout directly); read/written as ``list[float]`` either way.
- ``TZDateTime`` -> timezone-aware UTC on both (SQLite stores naive ISO; we normalise).
- tags/metadata -> ARRAY/JSONB on Postgres, JSON on SQLite (neither is queried in SQL;
  all metadata/tag filtering lives in the service layer).
"""

from __future__ import annotations

import struct
import uuid
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    JSON,
    DateTime,
    Float,
    Index,
    LargeBinary,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, declared_attr, mapped_column
from sqlalchemy.types import TypeDecorator

from .config import DEFAULT_EMBEDDING_DIM
from .db import Base

# Embedding dimension is fixed at the schema level; the runtime embedder must agree.
EMBEDDING_DIM = DEFAULT_EMBEDDING_DIM


class Embedding(TypeDecorator[list[float]]):
    """Vector on Postgres, little-endian float32 BLOB on SQLite; always a list[float]
    in Python. The SQLite BLOB layout is exactly what sqlite-vec's vec_distance_cosine
    expects, so nearest-neighbour search runs against the column directly."""

    impl = LargeBinary
    cache_ok = True

    class Comparator(TypeDecorator.Comparator[Any]):
        """Restores pgvector's cosine-distance operator (<=>) lost when wrapping
        Vector in a TypeDecorator. Only used on Postgres; SQLite nearest-search uses
        sqlite-vec's vec_distance_cosine directly in the repository."""

        def cosine_distance(self, other: Any) -> Any:
            return self.op("<=>", return_type=Float())(other)

    comparator_factory = Comparator

    def __init__(self, dim: int) -> None:
        self.dim = dim
        super().__init__()

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Vector(self.dim))
        return dialect.type_descriptor(LargeBinary())

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return struct.pack(f"<{len(value)}f", *value)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return list(value)
        return list(struct.unpack(f"<{len(value) // 4}f", value))


class TZDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend. SQLite's DateTime stores naive
    ISO strings, so we normalise: bind -> UTC, result -> attach UTC when naive."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class _EventColumns:
    """Shared column set for both event tables (identical shape)."""

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="info")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    jira_project: Mapped[str] = mapped_column(Text, nullable=False)
    jira_ticket: Mapped[str | None] = mapped_column(Text, nullable=True)

    @declared_attr
    def tags(cls) -> Mapped[list[str]]:
        return mapped_column(
            ARRAY(String).with_variant(JSON(), "sqlite"), nullable=False, default=list
        )

    @declared_attr
    def event_metadata(cls) -> Mapped[dict[str, Any]]:
        # DB column is ``metadata`` (the bare attr name collides with SQLAlchemy MetaData).
        return mapped_column(
            "metadata", JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=dict
        )

    supersedes: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    deletes: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    @declared_attr
    def embedding(cls) -> Mapped[list[float]]:
        return mapped_column(Embedding(EMBEDDING_DIM), nullable=False)

    exported_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class LocalEvent(_EventColumns, Base):
    """Records authored by this node's owner (authoritative, exportable).

    The pgvector HNSW index is created via raw ``CREATE INDEX IF NOT EXISTS`` in
    ``repository.init_schema`` (Postgres only), so it is intentionally absent here.
    """

    __tablename__ = "local_events"
    __table_args__ = (Index("ix_local_events_ts", "ts"),)


class TeamEvent(_EventColumns, Base):
    """Records ingested from other members (rebuildable cache)."""

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
        TZDateTime, nullable=False, server_default=func.now()
    )
