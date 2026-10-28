"""Real SQLite regressions for imported trust boundaries, recovery and sync."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest

from hivemind_app.config import Identity, Settings
from hivemind_app.db import create_engine, create_sessionmaker
from hivemind_app.embeddings import FakeEmbedder
from hivemind_app.loops import export_once, import_once
from hivemind_app.repository import Repository, init_schema
from hivemind_app.serialization import record_to_dict, serialize_records


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repository]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'storage.db'}")
    await init_schema(engine)
    yield Repository(create_sessionmaker(engine))
    await engine.dispose()


def publish(root: Path, author: str, name: str, text: str) -> Path:
    folder = root / author
    folder.mkdir(parents=True, exist_ok=True)
    file = folder / name
    file.write_text(text, encoding="utf-8")
    return file


@pytest.fixture
def record(make_record):  # type: ignore[no-untyped-def]
    return replace(make_record(), embedding=FakeEmbedder().embed("hello"))


@pytest.mark.parametrize("reference", ["supersedes", "deletes"])
@pytest.mark.parametrize("target_first", [True, False])
async def test_cross_author_references_never_hide_target(repo, record, reference, target_first):  # type: ignore[no-untyped-def]
    import uuid

    malicious = replace(
        record,
        id=uuid.uuid4(),
        author="bob",
        **{reference: record.id},
        event_type="tombstone" if reference == "deletes" else "note",
    )
    ordered = [record, malicious] if target_first else [malicious, record]
    for item in ordered:
        await repo.import_records([item])
    assert record.id not in await repo.hidden_ids()
    assert record.id in {r.id for r in await repo.recent_candidates(10)}
    assert [r.id for r in await repo.history_chain(record.id)] == [record.id]


async def test_owner_export_restores_local_and_is_not_reexported(repo, record, tmp_path):  # type: ignore[no-untyped-def]
    hive = tmp_path / "hive"
    publish(hive, "alice", "backup.ndjson", serialize_records([record]))
    assert await import_once(repo, Identity("alice"), hive) == 1
    assert await repo.get_by_id(record.id) is not None
    assert await repo.list_unexported_local() == []
    assert await export_once(repo, "alice", hive) == []


@pytest.mark.parametrize(
    "bad_vector", [[1.0, 2.0], [1e100] * 384, [float("nan")] * 384, [True] * 384]
)
async def test_bad_file_isolated_and_search_still_works(repo, record, tmp_path, bad_vector):  # type: ignore[no-untyped-def]
    import uuid

    hive = tmp_path / "hive"
    bad = record_to_dict(replace(record, id=uuid.uuid4(), author="bob"))
    bad["embedding"] = bad_vector
    publish(hive, "bob", "0-bad.ndjson", json.dumps(bad) + "\n")
    good = replace(record, author="bob")
    publish(hive, "bob", "1-good.ndjson", serialize_records([good]))
    assert await import_once(repo, Identity("alice"), hive) == 1
    assert (await repo.nearest_candidates(record.embedding, 10))[0][0].id == record.id
    assert "bob/0-bad.ndjson" not in await repo.list_ingested_files()


async def test_utf8_failure_does_not_starve_later_files(repo, record, tmp_path):  # type: ignore[no-untyped-def]
    hive = tmp_path / "hive"
    path = publish(hive, "bob", "0-bad.ndjson", "")
    path.write_bytes(b"\xff\xfe")
    publish(hive, "bob", "1-good.ndjson", serialize_records([record]))
    assert await import_once(repo, Identity("alice"), hive) == 1


async def test_conflicting_uuid_quarantined_in_either_order(repo, record, tmp_path):  # type: ignore[no-untyped-def]
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'other.db'}")
    await init_schema(engine)
    other = Repository(create_sessionmaker(engine))
    changed = replace(record, author="bob", title="different payload")
    try:
        for node, ordered in [(repo, [record, changed]), (other, [changed, record])]:
            for item in ordered:
                await node.import_records([item])
            assert await node.get_by_id(record.id) is None
            assert await node.recent_candidates(10) == []
            assert await node.import_records([record]) == 0
            assert await node.get_by_id(record.id) is None
    finally:
        await engine.dispose()


async def test_forked_history_includes_requested_anchor_and_all_branches(repo, record):  # type: ignore[no-untyped-def]
    import uuid

    branches = [replace(record, id=uuid.uuid4(), supersedes=record.id) for _ in range(3)]
    await repo.import_records([record, *branches])
    for anchor in [record, *branches]:
        assert {r.id for r in await repo.history_chain(anchor.id)} == {
            r.id for r in [record, *branches]
        }


async def test_bind_rejects_owner_hive_and_model_changes(repo):  # type: ignore[no-untyped-def]
    await repo.bind_identity("alice", "hive-1", "fake")
    await repo.bind_identity("alice", "hive-1", "fake")
    for args in [
        ("bob", "hive-1", "fake"),
        ("alice", "hive-2", "fake"),
        ("alice", "hive-1", "real"),
    ]:
        with pytest.raises(ValueError, match="differs"):
            await repo.bind_identity(*args)


async def test_model_mismatch_rejected_including_legacy_unknown(repo, record):  # type: ignore[no-untyped-def]
    await repo.bind_identity("alice", "hive-1", "fake")
    with pytest.raises(ValueError, match="embedding model"):
        await repo.import_records([record])
    with pytest.raises(ValueError, match="embedding model"):
        await repo.import_records([replace(record, embedding_model="other")])
    assert await repo.import_records([replace(record, embedding_model="fake")]) == 1


async def test_large_import_is_chunked(repo, record):  # type: ignore[no-untyped-def]
    import uuid

    rows = [replace(record, id=uuid.uuid4()) for _ in range(2200)]
    assert await repo.import_file(rows, "bob/large.ndjson") == 2200
    assert await repo.import_file(rows, "bob/large.ndjson") == 0
    assert len(await repo.recent_candidates(None)) == 2200


async def test_missing_hive_and_wrong_export_owner_leave_records_pending(repo, record, tmp_path):  # type: ignore[no-untyped-def]
    await repo.insert_local(record)
    hive = tmp_path / "missing"
    with pytest.raises(OSError):
        await export_once(repo, "alice", hive)
    assert not hive.exists()
    (hive / "bob").mkdir(parents=True)
    with pytest.raises(ValueError, match="identity"):
        await export_once(repo, "bob", hive)
    assert len(await repo.list_unexported_local()) == 1


async def test_roster_file_applies_when_environment_roster_empty(repo, record, tmp_path):  # type: ignore[no-untyped-def]
    hive = tmp_path / "hive"
    publish(hive, "mallory", "outsider.ndjson", serialize_records([record]))
    (hive / "roster.json").write_text('["alice", "bob"]')
    assert await import_once(repo, Identity("alice"), hive) == 0
    (hive / "roster.json").write_text('{"broken":true}')
    with pytest.raises(ValueError):
        await import_once(repo, Identity("alice"), hive)


def test_schema_dimension_must_match_configuration() -> None:
    with pytest.raises(ValueError, match="schema dimension"):
        Settings.from_env({"OWNER_HANDLE": "alice", "EMBEDDING_DIM": "16"})


async def test_zero_vectors_cannot_starve_nearest_pool(repo, record):  # type: ignore[no-untyped-def]
    import uuid

    zeros = [replace(record, id=uuid.uuid4(), embedding=[0.0] * 384) for _ in range(201)]
    await repo.import_records([record, *zeros])
    hits = await repo.nearest_candidates(record.embedding, 200)
    assert hits[0][0].id == record.id
    assert hits[0][1] == pytest.approx(0.0, abs=1e-6)
