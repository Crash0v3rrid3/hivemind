"""Context pack boundaries and actual MCP validation."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from hivemind_app.context import estimate_tokens, memory_context
from hivemind_app.mcp_server import _build_mcp
from hivemind_app.retrieval import RankedHit
from hivemind_app.schemas import SearchFilters
from hivemind_app.service import MemoryService


def service(records: list[Any]) -> MemoryService:
    return cast(MemoryService, SimpleNamespace(ranked_search=AsyncMock(return_value=records)))


@pytest.mark.parametrize("budget", [256, 257, 2000, 16000])
async def test_budget_unicode_and_sources(make_record, budget) -> None:  # type: ignore[no-untyped-def]
    record = make_record(body='😀漢字\n"\\' * 20000, metadata={"secret": "hidden"})
    svc = service([RankedHit(record, 1, ("semantic", "keyword"))])
    pack = await memory_context(svc, "source", token_budget=budget)
    assert estimate_tokens(pack) == pack["estimated_tokens"] <= budget
    assert pack["truncated"]
    assert pack["items"]
    item = pack["items"][0]
    assert record.body.startswith(item["excerpt"])
    assert item["source_id"] == str(record.id)
    assert item["author"] == record.author
    assert item["matched_channels"] == ["semantic", "keyword"]
    assert item["reason"] == "Retrieved via semantic, keyword; category: update"
    assert "metadata" not in json.dumps(pack) and "embedding" not in json.dumps(pack)


async def test_dedup_order_filters_and_long_titles(make_record) -> None:  # type: ignore[no-untyped-def]
    records = [
        make_record(title="First", body="\uff21  body"),
        make_record(title=" first ", body="a body"),
        make_record(title="x" * 10000),
        make_record(title="Last"),
    ]
    svc = service([RankedHit(r, 1 / (i + 1), ("keyword",)) for i, r in enumerate(records)])
    pack = await memory_context(svc, "task", "BETEL", "BETEL-1", 256)
    assert [i["source_id"] for i in pack["items"]] == [str(records[0].id), str(records[3].id)]
    assert pack["truncated"]
    svc.ranked_search.assert_awaited_once_with(  # type: ignore[attr-defined]
        "task",
        top_k=200,
        filters=SearchFilters(jira_project="BETEL", jira_ticket="BETEL-1"),
        mode="hybrid",
    )
    pack = await memory_context(svc, "task", max_items=1)
    assert len(pack["items"]) == 1 and pack["truncated"]


async def test_empty_and_task_envelope() -> None:
    svc = service([])
    pack = await memory_context(svc, "task", token_budget=256)
    assert not pack["truncated"] and not pack["items"]
    assert pack["estimated_tokens"] == estimate_tokens(pack)
    with pytest.raises(ValueError, match="envelope"):
        await memory_context(svc, "漢" * 1000, token_budget=256)


@pytest.mark.parametrize(
    "payload",
    [
        {"task": ""},
        {"task": " \n"},
        {"task": "x" * 10001},
        {"task": "漢" * 1000, "token_budget": 256},
        *[{"task": "task", "token_budget": v} for v in [True, 255, 16001, 256.0, "256"]],
        *[{"task": "task", "max_items": v} for v in [False, 0, 51, 1.0, "1"]],
        {"task": "task", "repo": "secret"},
        {"task": "task", "author": "bob"},
    ],
)
async def test_dispatch_rejects_invalid(payload) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ToolError):
        await _build_mcp(service([])).call_tool("memory_context", payload)


async def test_dispatch_serialized_envelope(make_record) -> None:  # type: ignore[no-untyped-def]
    svc = service([RankedHit(make_record(body="漢😀" * 1000), 1, ("keyword",))])
    result = await _build_mcp(svc).call_tool(
        "memory_context", {"task": "task", "token_budget": 256}
    )
    # FastMCP returns (text content, structured content) for structured tools.
    content = result[0] if isinstance(result, tuple) else result
    text = content[0].text  # type: ignore[index,union-attr]
    pack = json.loads(text)
    assert (len(text.encode("utf-8")) + 3) // 4 == pack["estimated_tokens"] <= 256
