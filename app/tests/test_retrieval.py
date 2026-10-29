"""Portable lexical SQL behavior, fusion, and actual MCP dispatch."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from hivemind_app.db import create_engine, create_sessionmaker
from hivemind_app.embeddings import FakeEmbedder, zero_vector
from hivemind_app.mcp_server import _build_mcp
from hivemind_app.repository import Repository, init_schema
from hivemind_app.retrieval import query_terms, reciprocal_rank_fusion
from hivemind_app.schemas import SearchFilters
from hivemind_app.service import MemoryService


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
async def repo(request, tmp_path):  # type: ignore[no-untyped-def]
    if request.param == "postgres":
        from conftest import pg_engine

        fixture = cast(Any, pg_engine).__wrapped__(request.getfixturevalue("database_url_test"))
        engine = await anext(fixture)
    else:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'lexical.db'}")
        await init_schema(engine)
    yield Repository(create_sessionmaker(engine))
    if request.param == "sqlite":
        await engine.dispose()
    else:
        await fixture.aclose()


@pytest.fixture
def make_record(make_record):  # type: ignore[no-untyped-def]
    def record(**kwargs):  # type: ignore[no-untyped-def]
        return make_record(embedding=zero_vector(384), **kwargs)

    return record


def test_terms_are_bounded_normalized_and_literal() -> None:
    assert query_terms("ABC-12 abc-12 src/foo.py foo_bar 50% C:\\path") == [
        "abc-12",
        "src/foo.py",
        "foo_bar",
        "50%",
        "c:\\path",
    ]
    assert len(query_terms(" ".join(f"term{i}" for i in range(100)))) == 32


def test_fusion_deduplicates_and_keeps_lexical_only(make_record) -> None:  # type: ignore[no-untyped-def]
    a, b, c = make_record(), make_record(), make_record()
    hits = reciprocal_rank_fusion({"semantic": [a, a, b], "keyword": [b, c]})
    assert hits[0].record == b
    assert hits[0].score == 1 / 62 + 1 / 61
    assert hits[0].channels == ("semantic", "keyword")
    assert {h.record.id for h in hits} == {a.id, b.id, c.id}


async def test_sql_ranking_filters_and_visibility(repo, make_record) -> None:  # type: ignore[no-untyped-def]
    ticket = make_record(jira_ticket="ENG-123", title="nothing")
    title = make_record(title="ENG-123")
    body = make_record(title="nothing", body="eng-123")
    tag = make_record(title="nothing", tags=["ENG-123"])
    hidden = make_record(title="ENG-123")
    deleted = make_record(title="ENG-123")
    conflict = make_record(title="ENG-123")
    wrong = make_record(title="ENG-123", author="bob")
    for record in [title, ticket, hidden, deleted, conflict, wrong]:
        await repo.insert_local(record)
    await repo.import_records([body, tag])
    await repo.import_records([replace(conflict, title="collision")])
    await repo.insert_local(make_record(title="new", supersedes=hidden.id))
    await repo.insert_local(make_record(event_type="tombstone", deletes=deleted.id))
    hits = await repo.keyword_candidates("Eng-123", 10, filters=SearchFilters(author="alice"))
    assert [r.id for r, _ in hits] == [ticket.id, title.id, tag.id, body.id]
    assert [
        r.id
        for r, _ in await repo.keyword_candidates(
            "ENG-123", 1, filters=SearchFilters(tags=["ENG-123"])
        )
    ] == [tag.id]


@pytest.mark.parametrize(
    "term", ["foo_bar", "50%", "C:\\path", "src/foo.py", "ENG-123", "Straße", "\uff21\uff22\uff23"]
)
async def test_exact_literal_tokens_and_ties(repo, make_record, term) -> None:  # type: ignore[no-untyped-def]
    a = make_record(title=term)
    b = make_record(title=term)
    decoy = make_record(title="fooXbar 500 C:Zpath srcXfooXpy ENGX123")
    for record in [a, b, decoy]:
        await repo.insert_local(record)
    hits = await repo.keyword_candidates(term, 2)
    assert [r.id for r, _ in hits] == sorted([a.id, b.id], key=str)


async def test_keyword_and_hybrid_dispatch(repo, make_record, identity) -> None:  # type: ignore[no-untyped-def]
    class NoEmbedding:
        dim = 384

        def embed(self, text: str) -> list[float]:
            raise AssertionError("keyword must not embed")

    record = make_record(title="src/foo.py")
    await repo.insert_local(record)
    svc = MemoryService(repo, NoEmbedding(), identity)
    mcp = _build_mcp(svc)
    result = await mcp.call_tool("memory_search", {"query": "src/foo.py", "mode": "keyword"})
    assert str(record.id) in str(result)
    assert "embedding" not in str(result)
    ranked = await svc.ranked_search("src/foo.py", mode="keyword")
    assert ranked[0].channels == ("keyword",)
    hybrid_mcp = _build_mcp(MemoryService(repo, FakeEmbedder(dim=384), identity))
    hybrid = await hybrid_mcp.call_tool("memory_search", {"query": "src/foo.py", "mode": "hybrid"})
    assert str(record.id) in str(hybrid)
    for payload in [{"query": "foo", "mode": "invalid"}, {"query": " ", "mode": "keyword"}]:
        with pytest.raises(ToolError):
            await mcp.call_tool("memory_search", payload)


async def test_hybrid_retains_lexical_only(fake_repo, fake_embedder, identity, make_record) -> None:  # type: ignore[no-untyped-def]
    semantic = make_record(title="semantic")
    keyword = make_record(title="keyword")
    fake_repo.nearest_override = [(semantic, 0.1)]

    async def lexical(query, pool, *, filters=None):  # type: ignore[no-untyped-def]
        assert pool == 200
        return [(keyword, 4.0)]

    fake_repo.keyword_candidates = lexical
    hits = await MemoryService(fake_repo, fake_embedder, identity).ranked_search(
        "query", mode="hybrid"
    )
    assert {hit.record.id for hit in hits} == {semantic.id, keyword.id}
    assert {hit.channels for hit in hits} == {("semantic",), ("keyword",)}


async def test_context_real_filters_and_visibility(repo, make_record, identity) -> None:  # type: ignore[no-untyped-def]
    from hivemind_app.context import memory_context

    visible = make_record(title="context", jira_ticket="BETEL-1")
    hidden = make_record(title="context", jira_ticket="BETEL-1")
    conflict = make_record(title="context", jira_ticket="BETEL-1")
    wrong = make_record(title="context", jira_ticket="BETEL-2")
    for record in [visible, hidden, conflict, wrong]:
        await repo.insert_local(record)
    await repo.import_records([replace(conflict, body="conflicting")])
    await repo.insert_local(make_record(event_type="tombstone", deletes=hidden.id))
    pack = await memory_context(
        MemoryService(repo, FakeEmbedder(dim=384), identity),
        "context",
        jira_project="BETEL",
        jira_ticket="BETEL-1",
    )
    assert [item["source_id"] for item in pack["items"]] == [str(visible.id)]
