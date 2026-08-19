"""MCP tool tests (SPEC §9, §14).

The tools are registered as closures over a :class:`MemoryService`. We build
them with ``_build_mcp`` against a service wired to the in-memory
``FakeRepository`` + ``FakeEmbedder`` and invoke the underlying coroutine
(``Tool.fn``) directly — asserting the identity invariant (author always OWNER,
author input rejected) and that filters plumb through to the service.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from hivemind_app.mcp_server import _build_mcp
from hivemind_app.service import MemoryService


def _tool_fns(mcp: object) -> dict[str, object]:
    """Return ``{tool_name: underlying coroutine}`` for a FastMCP instance.

    FastMCP exposes no public API to fetch a tool's underlying callable, so we
    reach into the version-private ``_tool_manager._tools[...].fn``. mcp is
    pinned ``>=1.2,<2`` (pyproject) and ``test_private_tool_access_contract``
    guards this shape, so an incompatible upgrade fails loudly here rather than
    obscurely across every tool test.
    """
    try:
        registry = mcp._tool_manager._tools  # type: ignore[attr-defined]
        return {name: tool.fn for name, tool in registry.items()}
    except AttributeError as exc:  # pragma: no cover - only trips on an mcp upgrade
        raise AssertionError(
            "FastMCP private tool-access path changed "
            "(_tool_manager._tools[name].fn); the pinned mcp SDK likely changed "
            "shape. Re-check the pin (mcp>=1.2,<2) and update _tool_fns()."
        ) from exc


@pytest.fixture
def tools(fake_repo, fake_embedder, identity, clock):  # type: ignore[no-untyped-def]
    service = MemoryService(fake_repo, fake_embedder, identity, clock=clock)
    return _tool_fns(_build_mcp(service))


def test_private_tool_access_contract(fake_repo, fake_embedder, identity) -> None:  # type: ignore[no-untyped-def]
    """Guard the version-private FastMCP internals the tool tests rely on.

    If a future mcp release renames ``_tool_manager`` / ``_tools`` / ``Tool.fn``,
    this fails with a clear message instead of the whole module breaking cryptically.
    """
    mcp = _build_mcp(MemoryService(fake_repo, fake_embedder, identity))
    assert hasattr(mcp, "_tool_manager"), "FastMCP no longer exposes _tool_manager"
    registry = mcp._tool_manager._tools
    assert registry, "no tools registered"
    sample = next(iter(registry.values()))
    assert hasattr(sample, "fn") and callable(sample.fn), "Tool.fn is no longer a callable"


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


def test_all_expected_tools_registered(tools) -> None:  # type: ignore[no-untyped-def]
    expected = {
        "memory_add",
        "memory_update",
        "memory_delete",
        "memory_get",
        "memory_list_recent",
        "memory_search",
        "memory_history",
        "memory_daily_summary",
        "me",
        "author_list",
    }
    assert expected.issubset(set(tools))


# ---------------------------------------------------------------------------
# me / author_list.
# ---------------------------------------------------------------------------


async def test_me_returns_owner(tools) -> None:  # type: ignore[no-untyped-def]
    assert await tools["me"]() == "alice"


async def test_author_list_returns_roster(tools) -> None:  # type: ignore[no-untyped-def]
    assert await tools["author_list"]() == ["alice", "bob", "cara"]


# ---------------------------------------------------------------------------
# memory_add — author always OWNER, author rejected.
# ---------------------------------------------------------------------------


async def test_add_author_is_always_owner(tools, fake_repo) -> None:  # type: ignore[no-untyped-def]
    out = await tools["memory_add"](title="t", body="b", jira_project="BETEL")
    assert out["author"] == "alice"
    assert fake_repo.local[0].author == "alice"
    assert "embedding" not in out  # never leak vectors


async def test_add_rejects_author_in_metadata(tools) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError):
        await tools["memory_add"](
            title="t", body="b", jira_project="BETEL", metadata={"author": "mallory"}
        )


async def test_add_has_no_author_parameter(tools) -> None:  # type: ignore[no-untyped-def]
    # The tool signature must not accept an author kwarg.
    with pytest.raises(TypeError):
        await tools["memory_add"](title="t", body="b", jira_project="BETEL", author="mallory")


async def test_add_validates_jira_project(tools) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError):
        await tools["memory_add"](title="t", body="b", jira_project="lower")


async def test_add_rejects_tombstone_event_type(tools) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError):
        await tools["memory_add"](title="t", body="b", jira_project="BETEL", event_type="tombstone")


# ---------------------------------------------------------------------------
# memory_update / memory_delete — own records only, author stays OWNER.
# ---------------------------------------------------------------------------


async def test_update_keeps_owner_author(tools, fake_repo) -> None:  # type: ignore[no-untyped-def]
    added = await tools["memory_add"](title="v1", body="b", jira_project="BETEL")
    updated = await tools["memory_update"](
        id=added["id"], title="v2", body="b", jira_project="BETEL"
    )
    assert updated["author"] == "alice"
    assert updated["supersedes"] == added["id"]


async def test_delete_emits_tombstone(tools) -> None:  # type: ignore[no-untyped-def]
    added = await tools["memory_add"](title="t", body="b", jira_project="BETEL")
    tomb = await tools["memory_delete"](id=added["id"])
    assert tomb["event_type"] == "tombstone"
    assert tomb["deletes"] == added["id"]


async def test_update_cannot_touch_foreign_record(tools, fake_repo, make_record) -> None:  # type: ignore[no-untyped-def]
    foreign = make_record(author="bob")
    await fake_repo.import_records([foreign])
    from hivemind_app.service import NotOwnRecordError

    with pytest.raises(NotOwnRecordError):
        await tools["memory_update"](id=str(foreign.id), title="t", body="b", jira_project="BETEL")


# ---------------------------------------------------------------------------
# memory_get / memory_history.
# ---------------------------------------------------------------------------


async def test_get_returns_record_then_none(tools) -> None:  # type: ignore[no-untyped-def]
    added = await tools["memory_add"](title="t", body="b", jira_project="BETEL")
    got = await tools["memory_get"](id=added["id"])
    assert got["id"] == added["id"]
    assert await tools["memory_get"](id=str(uuid.uuid4())) is None


async def test_history_chain(tools) -> None:  # type: ignore[no-untyped-def]
    v1 = await tools["memory_add"](title="v1", body="b", jira_project="BETEL")
    v2 = await tools["memory_update"](id=v1["id"], title="v2", body="b", jira_project="BETEL")
    chain = await tools["memory_history"](id=v1["id"])
    assert [r["id"] for r in chain] == [v1["id"], v2["id"]]


# ---------------------------------------------------------------------------
# Filters plumb through list_recent / search.
# ---------------------------------------------------------------------------


async def test_list_recent_filter_plumbs_through(tools) -> None:  # type: ignore[no-untyped-def]
    await tools["memory_add"](title="a", body="b", jira_project="BETEL", event_type="blocker")
    await tools["memory_add"](title="c", body="d", jira_project="BETEL", event_type="decision")
    result = await tools["memory_list_recent"](event_type="blocker")
    assert len(result) == 1
    assert result[0]["event_type"] == "blocker"


async def test_list_recent_author_filter(tools, fake_repo, make_record) -> None:  # type: ignore[no-untyped-def]
    await tools["memory_add"](title="mine", body="b", jira_project="BETEL")
    await fake_repo.import_records([make_record(author="bob", title="theirs")])
    result = await tools["memory_list_recent"](author="bob")
    assert len(result) == 1
    assert result[0]["author"] == "bob"


async def test_search_filter_plumbs_through(tools, fake_repo, make_record) -> None:  # type: ignore[no-untyped-def]
    await tools["memory_add"](title="mine", body="b", jira_project="BETEL")
    await fake_repo.import_records([make_record(author="bob", title="theirs")])
    result = await tools["memory_search"](query="anything", author="bob")
    assert all(r["author"] == "bob" for r in result)
    assert any(r["author"] == "bob" for r in result)


async def test_daily_summary_tool(tools) -> None:  # type: ignore[no-untyped-def]
    await tools["memory_add"](
        title="t", body="b", jira_project="BETEL", ts=datetime(2026, 8, 19, 10, tzinfo=UTC)
    )
    summary = await tools["memory_daily_summary"](day="2026-08-19")
    assert summary["total"] == 1
    assert summary["authors"]["alice"]["update"] == 1
