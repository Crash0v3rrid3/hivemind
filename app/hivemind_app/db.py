"""Async SQLAlchemy engine/sessionmaker and pgvector type registration.

The pgvector codec is registered on every new asyncpg connection via the
engine ``connect`` event so that ``vector`` columns round-trip as Python
lists. Schema DDL lives in :mod:`hivemind_app.repository` (idempotent init);
this module only wires up the engine and the declarative base.
"""

from __future__ import annotations

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
    """Create the async engine and attach the pgvector ``connect`` hook.

    SQLAlchemy's asyncpg adapter connection exposes ``run_async``, which runs a
    coroutine on the current greenlet during the synchronous ``connect`` event.
    ``pgvector.asyncpg.register_vector`` is a coroutine taking the raw asyncpg
    connection, so we hand it straight to ``run_async``.
    """
    engine = create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        future=True,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: object, _record: object) -> None:
        from pgvector.asyncpg import register_vector

        # ``run_async`` is provided by SQLAlchemy's AsyncAdapt asyncpg
        # connection wrapper; it awaits ``fn(driver_connection)`` inside the
        # active greenlet.
        dbapi_connection.run_async(register_vector)  # type: ignore[attr-defined]

    return engine


def create_sessionmaker(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Create an ``async_sessionmaker`` bound to ``engine``."""
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
