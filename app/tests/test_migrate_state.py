"""Legacy migration is explicit, atomic, and cannot change an existing identity."""

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select

from hivemind_app.config import Settings
from hivemind_app.db import create_engine, create_sessionmaker
from hivemind_app.migrate_state import migrate_legacy_state
from hivemind_app.models import StateBinding
from hivemind_app.repository import Repository, init_schema
from hivemind_app.serialization import EventRecord


async def test_legacy_migration_validates_owner_and_is_idempotent(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    settings = Settings.from_env({"OWNER_HANDLE": "alice", "EMBEDDING_MODEL": "fake"})
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    try:
        await init_schema(engine)
        repo = Repository(create_sessionmaker(engine))
        now = datetime.now(UTC)
        record = EventRecord(
            id=uuid4(),
            author="alice",
            event_type="note",
            status="info",
            title="old",
            body="body",
            jira_project="AB",
            jira_ticket=None,
            tags=[],
            metadata={},
            supersedes=None,
            deletes=None,
            ts=now,
            created_at=now,
            embedding=[0.0] * 384,
        )
        await repo.insert_local(record)
        await repo.mark_exported([record.id], now)
        with pytest.raises(ValueError, match="different owner"):
            await migrate_legacy_state(engine, replace(settings, owner_handle="bob"))
        async with create_sessionmaker(engine)() as session:
            assert not (await session.execute(select(StateBinding))).all()
        await migrate_legacy_state(engine, settings)
        await migrate_legacy_state(engine, settings)
        await repo.bind_identity(settings.owner_handle, settings.hive_id, settings.embedding_model)
        assert await repo.get_by_id(record.id) is not None
        assert await repo.list_unexported_local() == []
        await migrate_legacy_state(engine, settings, republish_own=True)
        assert [r.id for r in await repo.list_unexported_local()] == [record.id]
        with pytest.raises(ValueError, match="already bound"):
            await migrate_legacy_state(engine, replace(settings, hive_id="another-hive"))
    finally:
        await engine.dispose()
