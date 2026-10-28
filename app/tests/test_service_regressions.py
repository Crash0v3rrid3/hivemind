"""Regression coverage at real SQLite and MCP validation boundaries."""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from hivemind_app.config import Identity
from hivemind_app.db import create_engine, create_sessionmaker
from hivemind_app.embeddings import FakeEmbedder
from hivemind_app.mcp_server import _build_mcp
from hivemind_app.repository import Repository, init_schema
from hivemind_app.schemas import MemoryAddRequest, SearchFilters
from hivemind_app.serialization import EventRecord
from hivemind_app.service import MemoryService


@pytest.fixture
async def real_service(tmp_path):  # type: ignore[no-untyped-def]
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'regression.db'}")
    await init_schema(engine)
    repo = Repository(create_sessionmaker(engine))
    emb = FakeEmbedder(384)
    yield MemoryService(repo, emb, Identity("alice")), repo, emb
    await engine.dispose()


def record(emb: FakeEmbedder, **changes: object) -> EventRecord:
    now = datetime(2026, 8, 19, tzinfo=UTC)
    values = {
        "id": uuid.uuid4(),
        "author": "alice",
        "event_type": "update",
        "status": "info",
        "title": "test",
        "body": "",
        "jira_project": "TEST",
        "ts": now,
        "created_at": now,
        "embedding": emb.embed("test"),
    }
    values.update(changes)
    return EventRecord(**values)  # type: ignore[arg-type]


async def test_filters_precede_candidate_limit(real_service):  # type: ignore[no-untyped-def]
    svc, repo, emb = real_service
    target = record(emb, author="bob", tags=["needle"])
    noise = [record(emb, ts=target.ts + timedelta(days=1)) for _ in range(220)]
    await repo.import_records([target, *noise])
    filters = SearchFilters(author="bob", tags=["needle"])
    assert [r.id for r in await svc.list_recent(1, filters)] == [target.id]
    assert [r.id for r in await svc.search("test", 1, filters)] == [target.id]


async def test_hidden_rows_do_not_exhaust_candidate_pool(real_service):  # type: ignore[no-untyped-def]
    svc, repo, emb = real_service
    target = record(emb, ts=datetime(2026, 8, 18, tzinfo=UTC))
    hidden = [record(emb) for _ in range(220)]
    tombs = [record(emb, event_type="tombstone", deletes=r.id) for r in hidden]
    await repo.import_records([target, *hidden, *tombs])
    assert [r.id for r in await svc.list_recent(1)] == [target.id]
    assert [r.id for r in await svc.search("test", 1)] == [target.id]


async def test_historical_daily_summary_has_no_global_cutoff(real_service):  # type: ignore[no-untyped-def]
    svc, repo, emb = real_service
    target = record(emb)
    # More than the former hard cutoff, bulk-imported to exercise the real query.
    future = [record(emb, ts=target.ts + timedelta(days=1)) for _ in range(10001)]
    await repo.import_records([target, *future])
    assert await svc.daily_summary("2026-08-19") == {
        "day": "2026-08-19",
        "authors": {"alice": {"update": 1}},
        "total": 1,
    }


async def test_history_contains_both_branches_and_anchor(real_service):  # type: ignore[no-untyped-def]
    svc, repo, emb = real_service
    base = record(emb)
    left = record(emb, supersedes=base.id)
    right = record(emb, supersedes=base.id)
    await repo.import_records([base, left, right])
    for anchor in (base, left, right):
        assert {r.id for r in await svc.history(anchor.id)} == {base.id, left.id, right.id}


@pytest.mark.parametrize("limit", [-1, 0, 1001, True, 1.5])
async def test_service_rejects_invalid_read_limits(fake_repo, fake_embedder, identity, limit):  # type: ignore[no-untyped-def]
    svc = MemoryService(fake_repo, fake_embedder, identity)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        await svc.list_recent(limit)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        await svc.search("test", limit)


async def test_mcp_rejects_unknown_arguments_before_writing(fake_repo, fake_embedder, identity):  # type: ignore[no-untyped-def]
    mcp = _build_mcp(MemoryService(fake_repo, fake_embedder, identity))
    with pytest.raises(ToolError, match="Unexpected arguments: author"):
        await mcp.call_tool(
            "memory_add", {"title": "test", "body": "", "jira_project": "TEST", "author": "mallory"}
        )
    assert fake_repo.local == []
    assert all(tool.inputSchema["additionalProperties"] is False for tool in await mcp.list_tools())
    await mcp.call_tool("memory_add", {"title": "test", "body": "", "jira_project": "TEST"})
    assert fake_repo.local[0].author == "alice"


@pytest.mark.parametrize("limit", [-1, 0, 1001, True, 1.5, "5"])
async def test_mcp_read_limits_are_validated(fake_repo, fake_embedder, identity, limit):  # type: ignore[no-untyped-def]
    mcp = _build_mcp(MemoryService(fake_repo, fake_embedder, identity))
    with pytest.raises(ToolError):
        await mcp.call_tool("memory_list_recent", {"limit": limit})
    with pytest.raises(ToolError):
        await mcp.call_tool("memory_search", {"query": "test", "top_k": limit})


async def test_embedding_does_not_block_event_loop(fake_repo, fake_embedder, identity):  # type: ignore[no-untyped-def]
    typed_embedder: FakeEmbedder = fake_embedder
    started = threading.Event()
    released = threading.Event()

    class BlockingEmbedder:
        dim = fake_embedder.dim

        def embed(self, text: str) -> list[float]:
            started.set()
            # Safety timeout makes a regression fail instead of hanging pytest.
            if not released.wait(2):
                raise TimeoutError("event loop did not release the embedder")
            return typed_embedder.embed(text)

    svc = MemoryService(fake_repo, BlockingEmbedder(), identity)
    pending = asyncio.create_task(
        svc.add(MemoryAddRequest(title="test", body="", jira_project="TEST"))
    )
    try:
        async with asyncio.timeout(1):
            while not started.is_set():
                await asyncio.sleep(0.001)
        assert not pending.done()
    finally:
        released.set()
    await pending


async def test_http_mcp_rejects_unknown_author(tmp_path):  # type: ignore[no-untyped-def]
    import httpx
    from asgi_lifespan import LifespanManager

    from hivemind_app.config import Settings
    from hivemind_app.mcp_server import create_app

    cfg = Settings.from_env(
        {
            "OWNER_HANDLE": "alice",
            "EMBEDDING_MODEL": "fake",
            "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'http.db'}",
        }
    )
    app = create_app(cfg, start_loops=False)
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:7777",
            headers={"Accept": "application/json, text/event-stream"},
        ) as client,
    ):
        health = await client.get("/healthz")
        assert health.json() == {"service": "hivemind", "status": "ok", "owner": "alice"}
        initialized = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        client.headers["mcp-session-id"] = initialized.headers["mcp-session-id"]
        client.headers["mcp-protocol-version"] = "2025-03-26"
        await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
        )
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "memory_add",
                    "arguments": {
                        "title": "test",
                        "body": "",
                        "jira_project": "TEST",
                        "author": "mallory",
                    },
                },
            },
        )
        assert response.status_code == 200
        assert '"isError":true' in response.text
        assert "Unexpected arguments: author" in response.text


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "x" * 1001),
        ("body", "x" * 100001),
        ("tags", ["x"] * 101),
        ("tags", ["x" * 101]),
        ("metadata", {"text": "x" * 100001}),
    ],
)
async def test_mcp_rejects_oversized_write_payloads(  # type: ignore[no-untyped-def]
    fake_repo,
    fake_embedder,
    identity,
    field,
    value,
):
    mcp = _build_mcp(MemoryService(fake_repo, fake_embedder, identity))
    payload = {"title": "test", "body": "", "jira_project": "TEST", field: value}
    with pytest.raises(ToolError):
        await mcp.call_tool("memory_add", payload)
    assert fake_repo.local == []


@pytest.mark.parametrize("query", ["", "x" * 10001])
async def test_search_rejects_oversized_or_empty_query(  # type: ignore[no-untyped-def]
    fake_repo,
    fake_embedder,
    identity,
    query,
):
    svc = MemoryService(fake_repo, fake_embedder, identity)
    with pytest.raises(ValueError, match="query must contain"):
        await svc.search(query)
    with pytest.raises(ToolError):
        await _build_mcp(svc).call_tool("memory_search", {"query": query})
