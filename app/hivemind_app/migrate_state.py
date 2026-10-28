"""Explicitly attest the provenance of a stopped, backed-up legacy database.

Run with the SAME environment as the application, after reviewing its records:
python -m hivemind_app.migrate_state --confirm-provenance
This never changes an existing binding or rewrites records/vectors.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from .config import Settings
from .db import create_engine, create_sessionmaker
from .models import LocalEvent, StateBinding, TeamEvent
from .repository import init_schema
from .serialization import validate_embedding


async def migrate_legacy_state(
    engine: AsyncEngine, settings: Settings, *, republish_own: bool = False
) -> None:
    """Validate stored vectors and authors, then atomically add the binding.

    Hive and model provenance cannot be inferred from v1 vectors; the operator
    must attest them using the CLI flag. Run with the application stopped.
    """
    await init_schema(engine)
    expected = {
        "owner": settings.owner_handle,
        "hive_id": settings.hive_id or str(Path(settings.hivemind_path).resolve()),
        "embedding_model": settings.embedding_model,
    }
    async with create_sessionmaker(engine)() as session, session.begin():
        existing = dict((await session.execute(select(StateBinding.key, StateBinding.value))).all())
        if existing:
            if existing != expected:
                raise ValueError("database already bound to another owner, hive, or model")
            if republish_own:
                await session.execute(update(LocalEvent).values(exported_at=None))
            return
        for model in (LocalEvent, TeamEvent):
            result = await session.stream(select(model))
            async for raw_row in result.scalars():
                row = cast(LocalEvent | TeamEvent, raw_row)
                if model is LocalEvent and row.author != settings.owner_handle:
                    raise ValueError("legacy local records belong to a different owner")
                validate_embedding(list(row.embedding), settings.embedding_dim)
        session.add_all(StateBinding(key=k, value=v) for k, v in expected.items())
        if republish_own:
            await session.execute(update(LocalEvent).values(exported_at=None))


async def _run(republish_own: bool) -> None:
    settings = Settings.from_env()
    engine = create_engine(settings.database_url)
    try:
        await migrate_legacy_state(engine, settings, republish_own=republish_own)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-provenance",
        action="store_true",
        help="attest all stored records belong to configured hive/model and backup is complete",
    )
    parser.add_argument(
        "--republish-own",
        action="store_true",
        help="queue own records for new immutable files with explicit model provenance",
    )
    args = parser.parse_args()
    if not args.confirm_provenance:
        parser.error(
            "stop the app, back up the DB, verify provenance, then pass --confirm-provenance"
        )
    asyncio.run(_run(args.republish_own))
    print("Legacy database validated and bound; record payloads preserved.")


if __name__ == "__main__":
    main()
