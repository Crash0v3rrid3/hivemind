"""Local sync observations, bounded output and real dispatcher coverage."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace

import httpx
import pytest
from asgi_lifespan import LifespanManager
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import insert

from hivemind_app.config import Settings
from hivemind_app.db import create_engine, create_sessionmaker
from hivemind_app.embeddings import zero_vector
from hivemind_app.loops import export_once, import_once
from hivemind_app.mcp_server import _build_mcp, create_app
from hivemind_app.models import LocalEvent
from hivemind_app.repository import Repository, _record_to_values, init_schema
from hivemind_app.serialization import serialize_records
from hivemind_app.service import MemoryService
from hivemind_app.sync_status import SyncStatus


async def test_cycles(fake_repo, identity, hive_root, make_record):  # type: ignore[no-untyped-def]
    state = SyncStatus(False)
    assert state.cycles["import"]["last_attempt_at"] is None
    await export_once(fake_repo, "alice", hive_root, observer=state)
    folder = hive_root / "bob" / "2026-08-19"
    folder.mkdir(parents=True)
    (folder / "good.ndjson").write_text(serialize_records([make_record(author="bob")]))
    for i in range(25):
        (folder / f"bad{i}.ndjson").write_text("SECRET")
    assert await import_once(fake_repo, identity, hive_root, observer=state) == 1
    cycle = state.cycles["import"]
    assert cycle["problem_count"] == 25
    assert len(cycle["problems"]) == 20
    assert cycle["last_success_at"] is None
    assert "SECRET" not in json.dumps(cycle)
    assert str(hive_root) not in json.dumps(cycle)
    for path in folder.glob("bad*"):
        path.unlink()
    await import_once(fake_repo, identity, hive_root, observer=state)
    cycle = state.cycles["import"]
    success = cycle["last_success_at"]
    assert success is not None
    (hive_root / "roster.json").write_text("SECRET")
    with pytest.raises(ValueError):
        await import_once(fake_repo, replace(identity, roster=()), hive_root, observer=state)
    cycle = state.cycles["import"]
    assert cycle["last_failure_category"] == "roster"
    assert cycle["last_success_at"] == success
    await import_once(fake_repo, identity, hive_root / "missing", observer=state)
    cycle = state.cycles["import"]
    assert cycle["last_failure_category"] == "missing_mount"
    assert cycle["last_success_at"] == success
    await export_once(fake_repo, "alice", hive_root / "missing", observer=state)
    assert state.cycles["export"]["last_failure_category"] == "missing_mount"


async def test_counts_and_dispatch(tmp_path, make_record, fake_embedder, identity):  # type: ignore[no-untyped-def]
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'counts.db'}")
    try:
        await init_schema(engine)
        repo = Repository(create_sessionmaker(engine))
        async with create_sessionmaker(engine)() as session, session.begin():
            await session.execute(
                insert(LocalEvent),
                [_record_to_values(make_record(embedding=zero_vector(384))) for _ in range(1005)],
            )
        assert len(await repo.list_unexported_local()) == 1000
        rec = make_record(author="bob", embedding=zero_vector(384))
        await repo.import_records([rec, replace(rec, body="collision")])
        state = SyncStatus(False)
        mcp = _build_mcp(MemoryService(repo, fake_embedder, identity), state, repo)
        result = await mcp.call_tool("memory_sync_status", {})
        assert "1005" in str(result)
        snapshot = await state.snapshot(repo)
        assert snapshot["pending_local_exports"] == 1005
        assert snapshot["quarantined_ids"] == 1
        assert snapshot["health"] == "degraded"
        with pytest.raises(ToolError):
            await mcp.call_tool("memory_sync_status", {"secret": "SECRET"})
    finally:
        await engine.dispose()


async def test_get_startup(tmp_path):  # type: ignore[no-untyped-def]
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
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client,
    ):
        response = await client.get("/sync/status")
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "hivemind"
        assert data["health"] == "unknown"
        assert data["remote_delivery"] == "unknown"
        assert data["loops_enabled"] is False
        assert data["pending_local_exports"] == 0
        assert data["import"]["last_attempt_at"] is None


async def test_failures_preserve_success_and_redact(  # type: ignore[no-untyped-def]
    fake_repo, identity, hive_root, make_record, monkeypatch
):
    from hivemind_app import loops

    state = SyncStatus(True)
    await import_once(fake_repo, identity, hive_root, observer=state)
    previous = state.cycles["import"]["last_success_at"]
    folder = hive_root / "bob"
    folder.mkdir(exist_ok=True)
    (folder / "batch.ndjson").write_text(serialize_records([make_record(author="bob")]))

    async def fail(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("SECRET /private/root")

    monkeypatch.setattr(fake_repo, "import_file", fail)
    assert await import_once(fake_repo, identity, hive_root, observer=state) == 0
    assert state.cycles["import"]["last_success_at"] == previous
    assert state.cycles["import"]["last_failure_category"] == "import"

    def fail_folder(path, on_error):  # type: ignore[no-untyped-def]
        raise OSError("SECRET /private/root")

    monkeypatch.setattr(loops, "_ndjson_files", fail_folder)
    assert await import_once(fake_repo, identity, hive_root, observer=state) == 0
    assert state.cycles["import"]["last_success_at"] == previous
    assert state.cycles["import"]["last_failure_category"] == "read"
    await fake_repo.insert_local(make_record())
    monkeypatch.setattr(fake_repo, "mark_exported", fail)
    with pytest.raises(RuntimeError):
        await export_once(fake_repo, "alice", hive_root, observer=state)
    assert state.cycles["export"]["last_success_at"] is None
    assert state.cycles["export"]["last_failure_category"] == "export"
    assert "SECRET" not in json.dumps(state.cycles)
    assert "/private" not in json.dumps(state.cycles)


async def test_traversal_error_keeps_partial_cycle_degraded(  # type: ignore[no-untyped-def]
    fake_repo, identity, hive_root, make_record, monkeypatch
):
    import os
    from pathlib import Path

    state = SyncStatus(False)
    await import_once(fake_repo, identity, hive_root, observer=state)
    previous = state.cycles["import"]["last_success_at"]
    member = hive_root / "bob"
    blocked = member / "blocked"
    blocked.mkdir(parents=True)
    (member / "good.ndjson").write_text(serialize_records([make_record(author="bob")]))
    (blocked / "deferred.ndjson").write_text(serialize_records([make_record(author="bob")]))
    scandir = os.scandir

    def scan(path):  # type: ignore[no-untyped-def]
        if Path(path) == blocked:
            raise PermissionError(13, "SECRET unreadable", str(blocked))
        return scandir(path)

    monkeypatch.setattr(os, "scandir", scan)
    assert await import_once(fake_repo, identity, hive_root, observer=state) == 1
    cycle = state.cycles["import"]
    assert cycle["last_success_at"] == previous
    assert cycle["problem_count"] == 1
    assert cycle["problems"] == [{"path": "bob/blocked", "reason": "read"}]
    assert "SECRET" not in json.dumps(cycle)
    assert str(hive_root) not in json.dumps(cycle)


async def test_health_waits_for_clean_completion():  # type: ignore[no-untyped-def]
    class Counts:
        async def sync_counts(self) -> tuple[int, int]:
            return 0, 0

    state = SyncStatus(True)
    repo = Counts()
    state.begin("import")
    state.begin("export")
    assert (await state.snapshot(repo))["health"] == "unknown"
    state.finish("export")
    assert (await state.snapshot(repo))["health"] == "unknown"
    state.problem("import", "read", "bob/day")
    state.finish("import")
    assert (await state.snapshot(repo))["health"] == "degraded"
    state.begin("import")
    assert (await state.snapshot(repo))["health"] == "degraded"
    state.finish("import")
    assert (await state.snapshot(repo))["health"] == "ok"
    state.begin("import")
    state.problem("import", "read", "bob/day")
    state.finish("import")
    state.begin("import")
    assert (await state.snapshot(repo))["health"] == "degraded"
    state.finish("import")
    assert (await state.snapshot(repo))["health"] == "ok"


async def test_export_preflight_keeps_event_loop_available(  # type: ignore[no-untyped-def]
    fake_repo, hive_root, monkeypatch
):
    from pathlib import Path

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    event_loop_thread = threading.get_ident()
    original = Path.is_dir

    def held_is_dir(path):  # type: ignore[no-untyped-def]
        assert threading.get_ident() != event_loop_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5), "preflight worker was not released"
        return original(path)

    monkeypatch.setattr(Path, "is_dir", held_is_dir)
    task = asyncio.create_task(
        export_once(fake_repo, "alice", hive_root, observer=SyncStatus(True))
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        progressed = asyncio.Event()
        loop.call_soon(progressed.set)
        await asyncio.wait_for(progressed.wait(), timeout=2)
        assert not task.done()
    finally:
        release.set()
        await task


async def test_guidance_keeps_late_categories_and_retained_retry_failure():  # type: ignore[no-untyped-def]
    from hivemind_app.sync_status import GUIDANCE

    class Counts:
        async def sync_counts(self) -> tuple[int, int]:
            return 0, 0

    state = SyncStatus(True)
    state.begin("import")
    for i in range(20):
        state.problem("import", "parse", f"bob/{i}.ndjson")
    state.problem("import", "import", "bob/late.ndjson")
    state.finish("import")
    snapshot = await state.snapshot(Counts())
    assert snapshot["import"]["problem_count"] == 21
    assert len(snapshot["import"]["problems"]) == 20
    assert snapshot["recovery_guidance"] == [GUIDANCE["import"], GUIDANCE["parse"]]
    assert set(snapshot["import"]) == {
        "last_attempt_at",
        "last_success_at",
        "last_failure_category",
        "problem_count",
        "problems",
    }
    state.begin("import")
    retry = await state.snapshot(Counts())
    assert retry["health"] == "degraded"
    assert retry["import"]["problems"] == []
    assert retry["recovery_guidance"] == [GUIDANCE["import"]]
    state.finish("import")
    assert (await state.snapshot(Counts()))["recovery_guidance"] == []


async def test_path_contract_real_import(fake_repo, identity, hive_root):  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime
    from pathlib import Path

    class Counts:
        async def sync_counts(self) -> tuple[int, int]:
            return 7, 2

    state = SyncStatus(True, clock=lambda: datetime(2026, 10, 29, tzinfo=UTC))
    member = hive_root / "bob"
    member.mkdir(exist_ok=True)
    (member / "batch\\retry%\n雪.ndjson").write_text("broken\n")
    folder = member
    for _ in range(15):
        folder /= "a" * 240
    folder.mkdir(parents=True)
    for i in range(20):
        (folder / f"bad{i}.ndjson").write_text("broken\n")
    await import_once(fake_repo, identity, hive_root, observer=state)
    snapshot = await state.snapshot(Counts())
    fixture = Path(__file__).parents[2] / "cli/test/fixtures/sync-path-status.json"
    assert snapshot == json.loads(fixture.read_text())
    assert snapshot["import"]["problem_count"] == 21
    assert snapshot["import"]["problems"] == [
        {"path": "bob/batch%5Cretry%25%0A%E9%9B%AA.ndjson", "reason": "parse"}
    ]


async def test_maximum_samples_fit_transport_and_are_detached():  # type: ignore[no-untyped-def]
    from hivemind_app.sync_status import GUIDANCE

    class Counts:
        async def sync_counts(self) -> tuple[int, int]:
            return 7, 2

    state = SyncStatus(True)
    for direction in ("import", "export"):
        state.begin(direction)
        for i in range(40):
            state.problem(direction, list(GUIDANCE)[i % 7], "bob/" + "x" * 508)
        state.finish(direction)
    snapshot = await state.snapshot(Counts())
    assert len(json.dumps(snapshot).encode()) < 64 * 1024
    assert len(snapshot["import"]["problems"]) == 20
    assert snapshot["export"]["problem_count"] == 40
    snapshot["import"]["problems"][0]["reason"] = "mutated"
    snapshot["export"]["problem_count"] = 0
    assert await state.snapshot(Counts()) != snapshot
    assert state.cycles["export"]["problem_count"] == 40
    assert state.cycles["import"]["problems"][0]["reason"] == "missing_mount"


def test_path_bytes_are_reversibly_encoded() -> None:
    from urllib.parse import unquote_to_bytes

    state = SyncStatus(True)
    raw = b"bob/nonutf8-\xff.ndjson"
    state.problem("import", "parse", raw.decode(errors="surrogateescape"))
    path = state.cycles["import"]["problems"][0]["path"]
    assert unquote_to_bytes(path) == raw
