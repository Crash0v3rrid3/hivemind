"""End-to-end cross-node convergence (SPEC §7, §14).

Unlike ``test_loops`` (loop bodies over an in-memory ``FakeRepository``), these
tests wire TWO real nodes — each with its own SQLite+sqlite-vec database —
through a shared ``hive`` directory standing in for the Google Drive folder, and
drive the full round-trip: local write -> export file -> peer import -> visible,
database and all. They prove the premise the product rests on: a record written
on one node converges onto another, authorship is taken from the *folder* (never
the file), and one unparseable peer file cannot block the rest.

SQLite-backed so they run in the normal unit layer with no external server.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

pytest.importorskip("aiosqlite")
pytest.importorskip("sqlite_vec")

from hivemind_app.config import Identity
from hivemind_app.db import create_engine, create_sessionmaker
from hivemind_app.embeddings import FakeEmbedder
from hivemind_app.loops import export_once, import_once
from hivemind_app.repository import Repository, init_schema
from hivemind_app.serialization import EventRecord, batch_filename

EMB = FakeEmbedder(dim=384)
_ROSTER = ("alice", "bob")


def _rec(author: str = "alice", **overrides: object) -> EventRecord:
    title = str(overrides.pop("title", "title"))
    body = str(overrides.pop("body", "body"))
    ts = overrides.pop("ts", datetime(2026, 8, 19, 10, 0, 0, tzinfo=UTC))
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "author": author,
        "event_type": "update",
        "status": "info",
        "title": title,
        "body": body,
        "jira_project": "BETEL",
        "jira_ticket": None,
        "tags": [],
        "metadata": {},
        "supersedes": None,
        "deletes": None,
        "ts": ts,
        "created_at": ts,
        "embedding": EMB.embed(f"{title}\n\n{body}"),
    }
    defaults.update(overrides)
    return EventRecord(**defaults)  # type: ignore[arg-type]


@pytest.fixture
async def make_node(  # type: ignore[no-untyped-def]
    tmp_path,
) -> AsyncIterator[Callable[[str], Awaitable[Repository]]]:
    """Factory building independent real nodes; disposes every engine on teardown."""
    engines: list[AsyncEngine] = []

    async def _make(name: str) -> Repository:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / f'{name}.db'}")
        await init_schema(engine)
        engines.append(engine)
        return Repository(create_sessionmaker(engine))

    yield _make
    for engine in engines:
        await engine.dispose()


async def test_record_written_on_one_node_converges_to_another(  # type: ignore[no-untyped-def]
    make_node, tmp_path
) -> None:
    hive = tmp_path / "hive"
    alice = await make_node("alice")
    bob = await make_node("bob")

    rec = _rec(author="alice", title="migration deadlock", body="postgres hang")
    await alice.insert_local(rec)

    written = await export_once(alice, "alice", hive)
    assert len(written) == 1

    inserted = await import_once(bob, Identity(owner_handle="bob", roster=_ROSTER), hive)
    assert inserted == 1

    got = await bob.get_by_id(rec.id)
    assert got is not None
    assert got.id == rec.id
    assert got.author == "alice"  # stamped from the folder
    assert got.title == "migration deadlock"


async def test_authorship_comes_from_folder_not_file(  # type: ignore[no-untyped-def]
    make_node, tmp_path
) -> None:
    hive = tmp_path / "hive"
    alice = await make_node("alice")
    bob = await make_node("bob")

    # alice's node emits a record whose author FIELD is forged.
    forged = _rec(author="mallory", title="forged")
    await alice.insert_local(forged)
    await export_once(alice, "alice", hive)

    await import_once(bob, Identity(owner_handle="bob", roster=_ROSTER), hive)
    got = await bob.get_by_id(forged.id)
    assert got is not None
    assert got.author == "alice"  # folder wins, forged field ignored


async def test_one_unparseable_peer_file_does_not_block_the_rest(  # type: ignore[no-untyped-def]
    make_node, tmp_path
) -> None:
    hive = tmp_path / "hive"
    alice = await make_node("alice")
    bob = await make_node("bob")

    good = _rec(author="alice", title="good")
    await alice.insert_local(good)
    await export_once(alice, "alice", hive)

    # Drop a validly-named but unparseable file beside the good batch. It sorts
    # first (tiny epoch), so it is processed before the good file.
    day_folder = next((hive / "alice").iterdir())
    (day_folder / batch_filename(99999, uuid.uuid4())).write_text(
        "{ this is not valid json", encoding="utf-8"
    )

    identity = Identity(owner_handle="bob", roster=_ROSTER)
    inserted = await import_once(bob, identity, hive)
    assert inserted == 1  # the good record still lands
    assert await bob.get_by_id(good.id) is not None

    # Re-import inserts nothing new (good file already ingested).
    assert await import_once(bob, identity, hive) == 0


async def test_two_nodes_converge_bidirectionally(  # type: ignore[no-untyped-def]
    make_node, tmp_path
) -> None:
    hive = tmp_path / "hive"
    alice = await make_node("alice")
    bob = await make_node("bob")

    a_rec = _rec(author="alice", title="from alice")
    b_rec = _rec(author="bob", title="from bob")
    await alice.insert_local(a_rec)
    await bob.insert_local(b_rec)
    await export_once(alice, "alice", hive)
    await export_once(bob, "bob", hive)

    await import_once(alice, Identity(owner_handle="alice", roster=_ROSTER), hive)
    await import_once(bob, Identity(owner_handle="bob", roster=_ROSTER), hive)

    # Each node now holds BOTH records, each attributed to its true author.
    a_sees_b = await alice.get_by_id(b_rec.id)
    b_sees_a = await bob.get_by_id(a_rec.id)
    assert a_sees_b is not None and a_sees_b.author == "bob"
    assert b_sees_a is not None and b_sees_a.author == "alice"
