"""MCP server: FastMCP tools mounted on FastAPI with a lifespan.

The lifespan performs the idempotent schema init (``CREATE EXTENSION IF NOT
EXISTS vector`` -> ``create_all`` -> ``CREATE INDEX IF NOT EXISTS`` HNSW) and
then starts the export/import background loops; on shutdown it stops the loops
and disposes the engine.

Everything is built inside :func:`create_app` so importing this module never
opens a DB connection nor loads the embedding model. Tests build the app with a
:class:`~hivemind_app.embeddings.FakeEmbedder` and a throwaway engine and drive
it via ``asgi-lifespan``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ContentBlock, Tool
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncEngine

from .config import Identity, Settings
from .db import create_engine, create_sessionmaker
from .embeddings import Embedder, resolve_embedder
from .loops import run_export_loop, run_import_loop
from .repository import Repository, init_schema
from .schemas import (
    MAX_BODY_LENGTH,
    MAX_QUERY_LENGTH,
    MAX_TAGS,
    MAX_TITLE_LENGTH,
    EventOut,
    MemoryAddRequest,
    MemoryUpdateRequest,
    SearchFilters,
    Tag,
)
from .serialization import EventRecord
from .service import MAX_READ_LIMIT, MemoryService

log = structlog.get_logger(__name__)


def _public(record: EventRecord) -> dict[str, Any]:
    """Serialize a record for tool output (no embedding vector)."""
    return EventOut.model_validate(record).model_dump(mode="json")


class StrictFastMCP(FastMCP):
    """Reject unknown arguments at the actual tools/call boundary."""

    async def list_tools(self) -> list[Tool]:
        registered = await super().list_tools()
        for tool in registered:
            tool.inputSchema["additionalProperties"] = False
        return registered

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        for tool in await self.list_tools():
            if tool.name == name:
                unknown = arguments.keys() - tool.inputSchema.get("properties", {}).keys()
                if unknown:
                    raise ToolError(f"Unexpected arguments: {', '.join(sorted(unknown))}")
                break
        return await super().call_tool(name, arguments)


def _build_mcp(service: MemoryService) -> FastMCP:
    """Register all MCP tools against ``service`` and return the FastMCP app."""
    mcp: FastMCP = StrictFastMCP("hivemind")

    @mcp.tool()
    async def memory_add(
        title: Annotated[str, Field(min_length=1, max_length=MAX_TITLE_LENGTH)],
        body: Annotated[str, Field(max_length=MAX_BODY_LENGTH)],
        jira_project: str,
        jira_ticket: str | None = None,
        event_type: str = "update",
        status: str = "info",
        tags: Annotated[list[Tag] | None, Field(max_length=MAX_TAGS)] = None,
        metadata: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> dict[str, Any]:
        """Create a memory record (authored as this node's owner)."""
        req = MemoryAddRequest(
            title=title,
            body=body,
            jira_project=jira_project,
            jira_ticket=jira_ticket,
            event_type=event_type,
            status=status,
            tags=tags or [],
            metadata=metadata or {},
            ts=ts,
        )
        return _public(await service.add(req))

    @mcp.tool()
    async def memory_update(
        id: str,
        title: Annotated[str, Field(min_length=1, max_length=MAX_TITLE_LENGTH)],
        body: Annotated[str, Field(max_length=MAX_BODY_LENGTH)],
        jira_project: str,
        jira_ticket: str | None = None,
        event_type: str = "update",
        status: str = "info",
        tags: Annotated[list[Tag] | None, Field(max_length=MAX_TAGS)] = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Supersede one of your OWN records with a new revision."""
        req = MemoryUpdateRequest(
            id=uuid.UUID(id),
            title=title,
            body=body,
            jira_project=jira_project,
            jira_ticket=jira_ticket,
            event_type=event_type,
            status=status,
            tags=tags or [],
            metadata=metadata or {},
        )
        return _public(await service.update(req))

    @mcp.tool()
    async def memory_delete(id: str) -> dict[str, Any]:
        """Soft-delete one of your OWN records (emits a tombstone)."""
        return _public(await service.delete(uuid.UUID(id)))

    @mcp.tool()
    async def memory_get(id: str) -> dict[str, Any] | None:
        """Fetch a single record by id (from either table)."""
        record = await service.get(uuid.UUID(id))
        return _public(record) if record is not None else None

    @mcp.tool()
    async def memory_list_recent(
        limit: Annotated[int, Field(ge=1, le=MAX_READ_LIMIT, strict=True)] = 50,
        author: str | None = None,
        event_type: str | None = None,
        status: str | None = None,
        jira_project: str | None = None,
        jira_ticket: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        tags: Annotated[list[Tag] | None, Field(max_length=MAX_TAGS)] = None,
    ) -> list[dict[str, Any]]:
        """List the most recent visible records, narrowed by optional filters."""
        filters = SearchFilters(
            author=author,
            event_type=event_type,
            status=status,
            jira_project=jira_project,
            jira_ticket=jira_ticket,
            date_from=date_from,
            date_to=date_to,
            tags=tags,
        )
        records = await service.list_recent(limit=limit, filters=filters)
        return [_public(r) for r in records]

    @mcp.tool()
    async def memory_search(
        query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_LENGTH)],
        top_k: Annotated[int, Field(ge=1, le=MAX_READ_LIMIT, strict=True)] = 20,
        author: str | None = None,
        event_type: str | None = None,
        status: str | None = None,
        jira_project: str | None = None,
        jira_ticket: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        tags: Annotated[list[Tag] | None, Field(max_length=MAX_TAGS)] = None,
    ) -> list[dict[str, Any]]:
        """Semantic search over the whole team's memory."""
        filters = SearchFilters(
            author=author,
            event_type=event_type,
            status=status,
            jira_project=jira_project,
            jira_ticket=jira_ticket,
            date_from=date_from,
            date_to=date_to,
            tags=tags,
        )
        records = await service.search(query, top_k=top_k, filters=filters)
        return [_public(r) for r in records]

    @mcp.tool()
    async def memory_history(id: str) -> list[dict[str, Any]]:
        """Return the supersede chain for a record id, oldest first."""
        records = await service.history(uuid.UUID(id))
        return [_public(r) for r in records]

    @mcp.tool()
    async def memory_daily_summary(
        day: str | None = None, author: str | None = None
    ) -> dict[str, Any]:
        """Per-author, per-event-type counts of visible records for a day."""
        return await service.daily_summary(day=day, author=author)

    @mcp.tool()
    async def me() -> str:
        """Return this node's owner handle."""
        return service.owner_handle

    @mcp.tool()
    async def author_list() -> list[str]:
        """Return the configured roster of member handles."""
        return list(service.roster)

    return mcp


def create_app(
    settings: Settings | None = None,
    *,
    embedder: Embedder | None = None,
    engine: AsyncEngine | None = None,
    hive_root: str | None = None,
    start_loops: bool = True,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the FastAPI app hosting the MCP server and background loops.

    All dependencies are injectable for testing. When omitted, ``settings`` is
    read from the environment, ``engine`` is created from ``settings.database_url``,
    and ``embedder`` defaults to the (lazy) real model — unless
    ``EMBEDDING_MODEL=fake``, which selects the deterministic ``FakeEmbedder``
    (no torch, no downloads) for local testing and CI.
    """
    cfg = settings or Settings.from_env()
    emb = resolve_embedder(cfg.embedding_model, cfg.embedding_dim, embedder)
    eng = engine or create_engine(cfg.database_url)
    owns_engine = engine is None
    sessionmaker = create_sessionmaker(eng)
    repo = Repository(sessionmaker)
    identity = Identity(owner_handle=cfg.owner_handle, roster=cfg.roster)
    service = MemoryService(repo, emb, identity, clock=clock, embedding_model=cfg.embedding_model)
    root = hive_root if hive_root is not None else cfg.hivemind_path

    mcp = _build_mcp(service)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Enter the MCP session manager's lifespan (mounted sub-app lifespans
        # are not run automatically by Starlette).
        async with mcp_app.router.lifespan_context(mcp_app):
            await init_schema(eng)
            await repo.bind_identity(
                cfg.owner_handle, cfg.hive_id or str(Path(root).resolve()), cfg.embedding_model
            )
            log.info("hivemind.schema.ready")

            stop = asyncio.Event()
            tasks: list[asyncio.Task[None]] = []
            if start_loops:
                tasks = [
                    asyncio.create_task(
                        run_export_loop(
                            repo,
                            cfg.owner_handle,
                            root,
                            cfg.export_interval_sec,
                            clock=clock,
                            stop_event=stop,
                        )
                    ),
                    asyncio.create_task(
                        run_import_loop(
                            repo,
                            identity,
                            root,
                            cfg.import_interval_sec,
                            stop_event=stop,
                        )
                    ),
                ]
                log.info("hivemind.loops.started")
            try:
                yield
            finally:
                stop.set()
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                if owns_engine:
                    await eng.dispose()
                log.info("hivemind.shutdown.complete")

    app = FastAPI(title="Hivemind", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"service": "hivemind", "status": "ok", "owner": cfg.owner_handle}

    app.mount("/", mcp_app)
    return app


def run() -> None:
    """Console entrypoint: build the app from env and serve with uvicorn."""
    import uvicorn

    cfg = Settings.from_env()
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.mcp_host, port=cfg.mcp_port, log_level="info")
