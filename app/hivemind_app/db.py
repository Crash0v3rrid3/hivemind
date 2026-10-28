"""Async SQLAlchemy engine/sessionmaker and the declarative base.

The persistence backend is chosen entirely by the DATABASE_URL scheme:
``postgresql+asyncpg://`` (Postgres + pgvector, the default) or
``sqlite+aiosqlite://`` (embedded SQLite + sqlite-vec).

On Postgres, ``vector`` columns are handled by ``pgvector.sqlalchemy.Vector`` (via the
cross-dialect :class:`~hivemind_app.models.Embedding` type); we deliberately do NOT
register pgvector's asyncpg codec (it would double-encode and fails on a brand-new DB).
On SQLite, the ``sqlite-vec`` loadable extension is loaded on every new connection
(below) so ``vec_distance_cosine`` is available, and embeddings are stored as float32
BLOBs. Schema DDL lives in :mod:`hivemind_app.repository` (idempotent init).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create the async engine for the configured backend.

    Postgres and SQLite are both supported; the backend is chosen by the URL scheme.
    For SQLite, the sqlite-vec extension is loaded on every connection.
    """
    engine = create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        future=True,
    )
    if engine.dialect.name == "sqlite":
        _register_sqlite_vec(engine)
    return engine


def _register_sqlite_vec(engine: AsyncEngine) -> None:
    """Load the sqlite-vec extension on each new SQLite connection.

    The connect event fires with SQLAlchemy's async-adapter DBAPI connection; the real
    extension is loaded via the aiosqlite driver on its own worker thread.
    """
    import sqlite_vec

    @event.listens_for(engine.sync_engine, "connect")
    def _load(dbapi_connection: Any, _record: Any) -> None:
        async def load(driver: Any) -> None:
            await driver.enable_load_extension(True)
            try:
                await driver.load_extension(sqlite_vec.loadable_path())
            finally:
                await driver.enable_load_extension(False)

        dbapi_connection.run_async(load)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an ``async_sessionmaker`` bound to ``engine``."""
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
