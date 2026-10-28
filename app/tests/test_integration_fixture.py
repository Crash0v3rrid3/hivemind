"""Configured integration databases must fail loudly on initialization errors."""

from typing import Any, cast

import conftest
import pytest

from hivemind_app import db, repository


async def test_configured_database_schema_error_is_not_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Engine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = Engine()

    async def broken_schema(_engine: Any) -> None:
        raise RuntimeError("DDL regression")

    monkeypatch.setattr(db, "create_engine", lambda _url: engine)
    monkeypatch.setattr(repository, "init_schema", broken_schema)
    fixture = cast(Any, conftest.pg_engine).__wrapped__("postgresql+asyncpg://configured")
    with pytest.raises(RuntimeError, match="DDL regression"):
        await anext(fixture)
    assert engine.disposed
