"""Async SQLAlchemy engine/sessionmaker and the declarative base.

``vector`` columns are handled entirely by SQLAlchemy's ``pgvector.sqlalchemy.Vector``
type (bind Python list -> vector literal, parse result -> list). We deliberately do
NOT register pgvector's asyncpg codec: combined with the SQLAlchemy type it would
double-encode the value (the type already renders the literal), and it also fails on
a brand-new database before the extension exists. Schema DDL lives in
:mod:`hivemind_app.repository` (idempotent init).
"""

from __future__ import annotations

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
    """Create the async engine. Vector binding/result is handled by the ORM type."""
    return create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        future=True,
    )


def create_sessionmaker(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Create an ``async_sessionmaker`` bound to ``engine``."""
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
