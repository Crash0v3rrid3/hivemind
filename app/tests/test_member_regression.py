"""Released-feature scenarios over isolated, bound replicas and real sync files."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from hivemind_app.config import Identity
from hivemind_app.context import memory_context
from hivemind_app.db import Base, create_engine, create_sessionmaker
from hivemind_app.embeddings import FakeEmbedder
from hivemind_app.loops import export_once, import_once
from hivemind_app.mcp_server import _build_mcp
from hivemind_app.repository import Repository, init_schema
from hivemind_app.schemas import MemoryAddRequest, MemoryUpdateRequest, SearchFilters
from hivemind_app.serialization import EventRecord, serialize_records
from hivemind_app.service import MemoryService
from hivemind_app.sync_status import SyncStatus

ROSTER = ("alice", "bob", "cara")
NOW = datetime(2026, 8, 19, 10, tzinfo=UTC)
EMBEDDER = FakeEmbedder()


@dataclass
class Node:
    engine: AsyncEngine
    repo: Repository
    service: MemoryService
    identity: Identity
    hive: Path
    status: SyncStatus

    async def export(self) -> list[str]:
        return await export_once(
            self.repo,
            self.identity.owner_handle,
            self.hive,
            clock=lambda: NOW,
            observer=self.status,
        )

    async def ingest(self) -> int:
        return await import_once(self.repo, self.identity, self.hive, observer=self.status)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
async def nodes(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[Callable[[str, str], Awaitable[Node]]]:
    """Each member has its own DB/schema AND independently delivered hive view.

    PostgreSQL cleanup drops only random schemas created here; never truncates a
    shared database. Configured PostgreSQL errors fail rather than becoming skips.
    """
    engines: list[AsyncEngine] = []
    schemas: list[str] = []
    admin: AsyncEngine | None = None
    if request.param == "postgres":
        url = os.environ.get("DATABASE_URL_TEST")
        if not url:
            pytest.skip("DATABASE_URL_TEST unset: PostgreSQL scenario not executed")
        admin = create_engine(url)
        try:
            async with admin.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        except BaseException:
            await admin.dispose()
            raise

    async def make(owner: str, state: str = "main") -> Node:
        hive = tmp_path / f"view-{owner}"
        for member in ROSTER:
            (hive / member).mkdir(parents=True, exist_ok=True)
        if admin is None:
            engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / (owner + state + '.db')}")
        else:
            schema = "reg_" + uuid.uuid4().hex
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            schemas.append(schema)
            engine = create_async_engine(
                admin.url, connect_args={"server_settings": {"search_path": f"{schema},public"}}
            )
        engines.append(engine)
        if admin is not None:
            async with engine.begin() as conn:
                # Public remains on search_path for vector, but checkfirst=True
                # could discover public tables and silently reuse shared state.
                # This schema is newly created and owned by this fixture, so
                # emit every table explicitly before exercising production init.
                assert await conn.scalar(text("SELECT current_schema()")) == schema
                await conn.run_sync(lambda sync: Base.metadata.create_all(sync, checkfirst=False))
        await init_schema(engine)
        if admin is not None:
            async with engine.connect() as conn:
                for table in Base.metadata.tables:
                    # Resolve exactly the unqualified name used by repositories;
                    # fail before bind_identity or any other scenario write if
                    # it would address a fallback/public table.
                    actual_schema = await conn.scalar(
                        text(
                            "SELECT n.nspname FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE c.oid = to_regclass(:table)"
                        ),
                        {"table": table},
                    )
                    assert actual_schema == schema, (table, actual_schema, schema)
        repo = Repository(create_sessionmaker(engine))
        await repo.bind_identity(owner, "regression-hive", "fake")
        identity = Identity(owner, ROSTER)
        service = MemoryService(repo, EMBEDDER, identity, clock=lambda: NOW, embedding_model="fake")
        return Node(engine, repo, service, identity, hive, SyncStatus(False, clock=lambda: NOW))

    try:
        yield make
    finally:
        for engine in engines:
            await engine.dispose()
        if admin is not None:
            try:
                async with admin.begin() as conn:
                    for schema in schemas:
                        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            finally:
                await admin.dispose()


def deliver(source: Node, target: Node, paths: list[str] | None = None) -> None:
    """Copy only explicitly delivered immutable files; no shared node state."""
    for path in paths or [str(p) for p in source.hive.rglob("*.ndjson")]:
        original = Path(path)
        if not original.is_absolute():
            original = source.hive / original
        destination = target.hive / original.relative_to(source.hive)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)


async def add(node: Node, title: str) -> EventRecord:
    return await node.service.add(
        MemoryAddRequest(
            title=title,
            body=f"evidence for {title}",
            jira_project="ENG",
            jira_ticket="ENG-42",
            tags=["regression"],
            event_type="decision",
            status="open",
        )
    )


async def assert_visible(node: Node, expected: list[EventRecord]) -> None:
    ids = {r.id for r in expected}
    filters = SearchFilters(
        jira_project="ENG",
        jira_ticket="ENG-42",
        tags=["regression"],
        status="open",
        event_type="decision",
    )
    assert {r.id for r in await node.service.list_recent(filters=filters)} == ids
    for mode in ("semantic", "keyword", "hybrid"):
        hits = await node.service.search("evidence", filters=filters, mode=mode)
        assert {r.id for r in hits} == ids
    pack = await memory_context(node.service, "evidence", jira_project="ENG", token_budget=16000)
    assert {item["source_id"] for item in pack["items"]} == {str(i) for i in ids}
    summary = await node.service.daily_summary("2026-08-19")
    assert summary["total"] == len(ids)
    for author in ROSTER:
        assert {
            r.id for r in await node.service.list_recent(filters=SearchFilters(author=author))
        } == {r.id for r in expected if r.author == author}


async def test_three_members_offline_reordered_repeated_and_restart(nodes: Any) -> None:
    alice, bob, cara = [await nodes(owner, "main") for owner in ROSTER]
    original = await add(alice, "original")
    first = await alice.export()
    updated = await alice.service.update(
        MemoryUpdateRequest(
            id=original.id,
            title="replacement",
            body="evidence replacement",
            jira_project="ENG",
            jira_ticket="ENG-42",
            tags=["regression"],
            event_type="decision",
            status="open",
        )
    )
    second = await alice.export()
    removed = await add(bob, "removed")
    third = await bob.export()
    await bob.service.delete(removed.id)
    fourth = await bob.export()
    survivor = await add(cara, "offline survivor")
    await cara.export()
    # Updates/tombstones arrive before originals on an offline member.
    deliver(alice, cara, second)
    deliver(bob, cara, fourth)
    await cara.ingest()
    await assert_visible(cara, [updated, survivor])
    deliver(alice, cara, first)
    deliver(bob, cara, third)
    await cara.ingest()
    for source in (alice, bob, cara):
        for target in (alice, bob, cara):
            if source is not target:
                deliver(source, target)
    for node in (alice, bob, cara):
        await node.ingest()
        await assert_visible(node, [updated, survivor])
        assert [r.id for r in await node.service.history(original.id)] == sorted(
            [original.id, updated.id], key=str
        )
        assert await node.ingest() == 0
        assert await node.export() == []
        assert (await node.status.snapshot(node.repo))["health"] == "ok"
    # New service/repository over the same engine models process restart on both backends.
    await cara.engine.dispose()
    restarted = Repository(create_sessionmaker(cara.engine))
    await restarted.bind_identity("cara", "regression-hive", "fake")
    cara.repo = restarted
    cara.service = MemoryService(restarted, EMBEDDER, cara.identity, embedding_model="fake")
    cara.status = SyncStatus(False)
    await assert_visible(cara, [updated, survivor])
    assert (await cara.status.snapshot(restarted))["health"] == "unknown"
    assert await cara.ingest() == 0
    # Lost owner DB recovers its own rows as exported alongside peer cache rows.
    recovered = await nodes("alice", "recovered")
    await recovered.ingest()
    await assert_visible(recovered, [updated, survivor])
    assert await recovered.repo.list_unexported_local() == []
    assert await recovered.export() == []


async def test_trust_conflicts_and_exclusion_across_members(nodes: Any) -> None:
    alice, bob, cara = [await nodes(owner, "main") for owner in ROSTER]
    original = await add(alice, "trusted original")
    await alice.export()
    deliver(alice, bob)
    await bob.ingest()
    malicious = replace(
        original,
        id=uuid.uuid4(),
        author="alice",
        supersedes=original.id,
        title="forged cross-author",
        body="evidence forgery",
    )
    (bob.hive / "bob" / "forged.ndjson").write_text(serialize_records([malicious]))
    outsider = replace(original, id=uuid.uuid4(), title="outsider")
    (bob.hive / "mallory").mkdir()
    (bob.hive / "mallory" / "ignored.ndjson").write_text(serialize_records([outsider]))
    deliver(bob, cara)
    await cara.ingest()
    stamped = await cara.service.get(malicious.id)
    assert stamped is not None and stamped.author == "bob"
    assert original.id not in await cara.repo.hidden_ids()
    assert await cara.service.get(outsider.id) is None
    assert [r.id for r in await cara.service.history(original.id)] == [original.id]
    collision = replace(original, title="conflicting UUID", author="cara")
    (cara.hive / "cara" / "collision.ndjson").write_text(serialize_records([collision]))
    for target in (alice, bob):
        deliver(cara, target)
    for node in (alice, bob, cara):
        await node.ingest()
        assert await node.service.get(original.id) is None
        await assert_visible(node, [replace(malicious, author="bob")])
        status = await node.status.snapshot(node.repo)
        assert status["quarantined_ids"] == 1 and status["health"] == "degraded"
        assert await node.ingest() == 0


@pytest.mark.parametrize("failure", ["publication", "mark_exported", "import_transaction"])
async def test_failed_sync_retry_preserves_data(
    nodes: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from hivemind_app import loops

    alice, bob = await nodes("alice", "main"), await nodes("bob", "main")
    records = [await add(alice, title) for title in ("first", "second")]
    assert (await alice.status.snapshot(alice.repo))["pending_local_exports"] == 2
    if failure == "import_transaction":
        await alice.export()
        deliver(alice, bob)
        # Failure after INSERT but before marker commit must roll back both rows.
        from sqlalchemy import event

        def fail_marker(
            conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
        ) -> None:
            if statement.lstrip().upper().startswith("INSERT INTO INGESTED_FILES"):
                raise RuntimeError("injected database failure")

        event.listen(bob.engine.sync_engine, "before_cursor_execute", fail_marker)
        try:
            assert await bob.ingest() == 0
        finally:
            event.remove(bob.engine.sync_engine, "before_cursor_execute", fail_marker)
        assert await bob.repo.list_ingested_files() == set()
        assert await bob.service.list_recent() == []
        assert (await bob.status.snapshot(bob.repo))["import"]["last_failure_category"] == "import"
    else:

        def fail_publish(*args: Any, **kwargs: Any) -> None:
            raise OSError("injected publication failure")

        async def fail_mark(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("injected marker failure")

        with monkeypatch.context() as patch:
            if failure == "publication":
                patch.setattr(loops, "_publish", fail_publish)
            else:
                patch.setattr(alice.repo, "mark_exported", fail_mark)
            with pytest.raises((OSError, RuntimeError)):
                await alice.export()
        snapshot = await alice.status.snapshot(alice.repo)
        assert snapshot["health"] == "degraded" and snapshot["pending_local_exports"] == 2
        assert snapshot["export"]["last_success_at"] is None
        await alice.export()
        deliver(alice, bob)
    await bob.ingest()
    await assert_visible(bob, records)
    assert await bob.ingest() == 0
    await bob.export()
    assert (await bob.status.snapshot(bob.repo))["health"] == "ok"
    await alice.ingest()
    assert (await alice.status.snapshot(alice.repo))["pending_local_exports"] == 0


@pytest.mark.parametrize("suffix", ['{"id":', '{"invalid":true}\n', "not-json\n"])
async def test_incomplete_and_malformed_batches_are_atomic_retryable(
    nodes: Any, suffix: str
) -> None:
    alice, bob = await nodes("alice", "main"), await nodes("bob", "main")
    good = await add(alice, "batch survivor")
    batch = bob.hive / "alice" / "batch.ndjson"
    wire = serialize_records([good])
    batch.write_text(wire + suffix)
    (bob.hive / "alice" / "interrupted.ndjson.tmp").write_text(wire)
    assert await bob.ingest() == 0
    assert await bob.service.list_recent() == []
    assert await bob.repo.list_ingested_files() == set()
    status = await bob.status.snapshot(bob.repo)
    assert status["health"] == "degraded" and status["import"]["problem_count"] == 1
    batch.write_text(wire)
    assert await bob.ingest() == 1
    assert await bob.ingest() == 0
    await assert_visible(bob, [good])
    await bob.export()
    assert (await bob.status.snapshot(bob.repo))["health"] == "ok"


async def test_actual_dispatcher_all_tools_and_unknown_arguments(nodes: Any) -> None:
    node = await nodes("alice", "main")
    mcp = _build_mcp(node.service, node.status, node.repo)

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        result = await mcp.call_tool(name, arguments)
        assert not isinstance(result, dict)
        if isinstance(result, tuple):
            structured = result[1]
            return structured["result"] if set(structured) == {"result"} else structured
        # Earlier supported MCP 1.x versions return only content blocks.
        blocks = cast(list[Any], result)
        if name == "me":
            return blocks[0].text
        if name == "author_list":
            return [block.text for block in blocks]
        parsed = [json.loads(block.text) for block in blocks]
        return (
            parsed
            if name in {"memory_history", "memory_search", "memory_list_recent"}
            else parsed[0]
        )

    initial = await call(
        "memory_add", {"title": "dispatcher", "body": "evidence", "jira_project": "ENG"}
    )
    updated = await call(
        "memory_update",
        {
            "id": initial["id"],
            "title": "dispatcher updated",
            "body": "evidence",
            "jira_project": "ENG",
        },
    )
    assert updated["author"] == "alice" and updated["supersedes"] == initial["id"]
    assert (await call("memory_get", {"id": updated["id"]}))["id"] == updated["id"]
    assert {r["id"] for r in await call("memory_history", {"id": initial["id"]})} == {
        initial["id"],
        updated["id"],
    }
    assert [r["id"] for r in await call("memory_list_recent", {"author": "alice"})] == [
        updated["id"]
    ]
    for mode in ("semantic", "keyword", "hybrid"):
        assert [
            r["id"] for r in await call("memory_search", {"query": "evidence", "mode": mode})
        ] == [updated["id"]]
    assert (await call("memory_daily_summary", {"day": "2026-08-19"}))["total"] == 1
    assert (await call("memory_context", {"task": "evidence"}))["items"][0]["source_id"] == updated[
        "id"
    ]
    assert await call("me", {}) == "alice"
    assert await call("author_list", {}) == list(ROSTER)
    assert (await call("memory_sync_status", {}))["pending_local_exports"] == 2
    tombstone = await call("memory_delete", {"id": updated["id"]})
    assert tombstone["deletes"] == updated["id"]
    assert await call("memory_list_recent", {}) == []
    registered = await mcp.list_tools()
    assert {t.name for t in registered} == {
        "memory_add",
        "memory_update",
        "memory_delete",
        "memory_get",
        "memory_list_recent",
        "memory_search",
        "memory_history",
        "memory_daily_summary",
        "memory_context",
        "memory_sync_status",
        "me",
        "author_list",
    }
    for tool in registered:
        with pytest.raises(ToolError, match="Unexpected arguments"):
            await mcp.call_tool(tool.name, {"author_impersonation": "mallory"})
    invalid_arguments: list[tuple[str, dict[str, Any]]] = [
        ("memory_add", {}),
        ("memory_update", {"id": "bad"}),
        ("memory_delete", {"id": "bad"}),
        ("memory_get", {"id": []}),
        ("memory_history", {"id": "bad"}),
        ("memory_list_recent", {"limit": True}),
        ("memory_daily_summary", {"day": "not-a-day"}),
        ("memory_context", {"task": "x", "token_budget": True}),
        ("memory_search", {"query": "x", "top_k": True}),
        (
            "memory_add",
            {"title": "x", "body": "x", "jira_project": "ENG", "metadata": {"author": "mallory"}},
        ),
    ]
    for name, arguments in invalid_arguments:
        with pytest.raises(ToolError):
            await mcp.call_tool(name, arguments)


@pytest.mark.parametrize("seed", [0, 19, 307])
async def test_generated_delivery_schedules_have_independent_expected_state(
    nodes: Any, seed: int
) -> None:
    """Bounded schedule variation; expected survivors follow authored operations.

    The oracle is an explicit ledger of final authored versions, not a second
    implementation of repository visibility or a comparison of replicas alone.
    """
    import random

    rng = random.Random(seed)
    replicas = [await nodes(owner, "main") for owner in ROSTER]
    surviving: list[EventRecord] = []
    publications: list[tuple[Node, str]] = []
    for node in replicas:
        keep = await add(node, node.identity.owner_handle + " keep")
        old = await add(node, node.identity.owner_handle + " old")
        deleted = await add(node, node.identity.owner_handle + " deleted")
        publications.extend((node, path) for path in await node.export())
        replacement = await node.service.update(
            MemoryUpdateRequest(
                id=old.id,
                title=node.identity.owner_handle + " final",
                body="evidence final",
                jira_project="ENG",
                jira_ticket="ENG-42",
                tags=["regression"],
                event_type="decision",
                status="open",
            )
        )
        await node.service.delete(deleted.id)
        publications.extend((node, path) for path in await node.export())
        surviving.extend([keep, replacement])
    deliveries = [
        (source, target, path)
        for source, path in publications
        for target in replicas
        if target is not source
    ]
    # Repeated delivery has no extra semantic effect; schedules contain all files.
    deliveries += deliveries[::3]
    rng.shuffle(deliveries)
    for source, target, path in deliveries:
        deliver(source, target, [path])
        await target.ingest()
    for node in replicas:
        await assert_visible(node, surviving)
        assert await node.ingest() == 0
        assert await node.export() == []
        assert await node.repo.sync_counts() == (0, 0)


async def test_pending_conflicting_owner_evidence_reaches_all_replicas(nodes: Any) -> None:
    alice, bob, cara = [await nodes(owner, "main") for owner in ROSTER]
    pending = await add(alice, "pending owner evidence")
    collision = replace(pending, author="bob", title="collision evidence")
    await bob.repo.insert_local(collision)
    await bob.export()
    deliver(bob, alice)
    deliver(bob, cara)
    await alice.ingest()
    await cara.ingest()
    assert await alice.repo.sync_counts() == (1, 1)
    assert await alice.service.get(pending.id) is None
    assert (await cara.service.get(pending.id)) is not None
    assert len(await alice.export()) == 1
    for target in (bob, cara):
        deliver(alice, target)
    for node in (alice, bob, cara):
        await node.ingest()
        await assert_visible(node, [])
        assert await node.repo.sync_counts() == (0, 1)
        assert (await node.status.snapshot(node.repo))["health"] == "degraded"
        assert await node.export() == []


async def test_background_export_retries_failure_and_stops_cleanly(
    nodes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from hivemind_app.loops import run_export_loop

    node = await nodes("alice", "main")
    record = await add(node, "background retry")
    stop = asyncio.Event()
    mark = node.repo.mark_exported
    failed = False

    async def mark_then_stop(ids: list[uuid.UUID], now: datetime) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("injected first publication bookkeeping failure")
        await mark(ids, now)
        stop.set()

    monkeypatch.setattr(node.repo, "mark_exported", mark_then_stop)
    await asyncio.wait_for(
        run_export_loop(node.repo, "alice", node.hive, 0, stop_event=stop, observer=node.status),
        timeout=5,
    )
    assert await node.repo.list_unexported_local() == []
    assert (await node.status.snapshot(node.repo))["export"]["last_failure_category"] is None
    # Both published files carry the same evidence after failed bookkeeping.
    peer = await nodes("bob", "main")
    deliver(node, peer)
    assert await peer.ingest() == 1
    await assert_visible(peer, [record])
    # A pre-set stop never starts another cycle or publishes new state.
    await add(node, "left pending after stop")
    await run_export_loop(node.repo, "alice", node.hive, 0, stop_event=stop)
    assert len(await node.repo.list_unexported_local()) == 1


async def test_member_tables_and_rows_are_independent_before_delivery(nodes: Any) -> None:
    """Public tables left by other integration tests cannot become replica state."""
    replicas = [await nodes(owner, "main") for owner in ROSTER]
    resolved_tables: set[int] = set()
    resolved_schemas: set[str] = set()
    for node in replicas:
        if node.engine.dialect.name == "postgresql":
            async with node.engine.connect() as conn:
                schema = await conn.scalar(text("SELECT current_schema()"))
                assert schema not in resolved_schemas and schema.startswith("reg_")
                resolved_schemas.add(schema)
                for table in Base.metadata.tables:
                    oid = await conn.scalar(
                        text("SELECT to_regclass(:table)::oid"), {"table": table}
                    )
                    assert oid is not None and oid not in resolved_tables
                    resolved_tables.add(oid)
        assert await node.repo.sync_counts() == (0, 0)
        assert await node.repo.list_ingested_files() == set()
    authored = [await add(node, node.identity.owner_handle + " isolated") for node in replicas]
    for node, own in zip(replicas, authored, strict=True):
        await assert_visible(node, [own])
        assert await node.repo.sync_counts() == (1, 0)
        for foreign in authored:
            if foreign is not own:
                assert await node.service.get(foreign.id) is None
    if replicas[0].engine.dialect.name == "postgresql":
        assert len(resolved_schemas) == 3
        assert len(resolved_tables) == 3 * len(Base.metadata.tables)
