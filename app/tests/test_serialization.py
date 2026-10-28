"""Tests for NDJSON serialization, atomic write, and path helpers (SPEC §5, §14)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hivemind_app.serialization import (
    LINE_KEYS,
    atomic_write_text,
    author_from_path,
    batch_filename,
    day_bucket,
    day_dir,
    dict_to_record,
    dt_to_iso,
    is_ndjson_file,
    iso_to_dt,
    parse_ndjson,
    record_to_dict,
    record_to_line,
    serialize_records,
)

# ---------------------------------------------------------------------------
# datetime helpers.
# ---------------------------------------------------------------------------


def test_dt_to_iso_uses_z_suffix() -> None:
    dt = datetime(2026, 8, 19, 10, 0, 0, tzinfo=UTC)
    assert dt_to_iso(dt) == "2026-08-19T10:00:00Z"


def test_dt_to_iso_naive_treated_as_utc() -> None:
    dt = datetime(2026, 8, 19, 10, 0, 0)
    assert dt_to_iso(dt) == "2026-08-19T10:00:00Z"


def test_iso_to_dt_parses_z() -> None:
    dt = iso_to_dt("2026-08-19T10:00:00Z")
    assert dt == datetime(2026, 8, 19, 10, 0, 0, tzinfo=UTC)
    assert dt.tzinfo is not None


def test_iso_to_dt_parses_offset() -> None:
    dt = iso_to_dt("2026-08-19T12:00:00+02:00")
    assert dt == datetime(2026, 8, 19, 10, 0, 0, tzinfo=UTC)


def test_iso_roundtrip() -> None:
    dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert iso_to_dt(dt_to_iso(dt)) == dt


# ---------------------------------------------------------------------------
# Day bucketing.
# ---------------------------------------------------------------------------


def test_day_bucket_utc() -> None:
    assert day_bucket(datetime(2026, 8, 19, 23, 59, tzinfo=UTC)) == "2026-08-19"


def test_day_bucket_converts_to_utc_before_bucketing() -> None:
    from datetime import timedelta, timezone

    # 2026-08-20 01:00 +03:00 == 2026-08-19 22:00 UTC -> day 2026-08-19.
    tz = timezone(timedelta(hours=3))
    ts = datetime(2026, 8, 20, 1, 0, tzinfo=tz)
    assert day_bucket(ts) == "2026-08-19"


# ---------------------------------------------------------------------------
# Record <-> dict / line.
# ---------------------------------------------------------------------------


def test_record_to_dict_has_exact_line_keys(make_record) -> None:  # type: ignore[no-untyped-def]
    obj = record_to_dict(make_record(embedding_model="fake"))
    assert tuple(obj.keys()) == LINE_KEYS


def test_record_to_dict_serializes_uuids_and_times(make_record) -> None:  # type: ignore[no-untyped-def]
    sup = uuid.uuid4()
    record = make_record(supersedes=sup, jira_ticket="BETEL-1", tags=["a"])
    obj = record_to_dict(record)
    assert obj["id"] == str(record.id)
    assert obj["supersedes"] == str(sup)
    assert obj["deletes"] is None
    assert obj["ts"].endswith("Z")
    assert obj["jira_ticket"] == "BETEL-1"


def test_line_is_compact_json(make_record) -> None:  # type: ignore[no-untyped-def]
    line = record_to_line(make_record())
    assert "\n" not in line
    assert ", " not in line  # compact separators
    json.loads(line)  # valid JSON


def test_dict_to_record_roundtrip(make_record) -> None:  # type: ignore[no-untyped-def]
    original = make_record(
        jira_ticket="BETEL-9",
        tags=["x", "y"],
        metadata={"k": 1, "nested": {"a": [1, 2]}},
        supersedes=uuid.uuid4(),
    )
    back = dict_to_record(record_to_dict(original))
    assert back.id == original.id
    assert back.author == original.author
    assert back.event_type == original.event_type
    assert back.status == original.status
    assert back.title == original.title
    assert back.body == original.body
    assert back.jira_project == original.jira_project
    assert back.jira_ticket == original.jira_ticket
    assert back.tags == original.tags
    assert back.metadata == original.metadata
    assert back.supersedes == original.supersedes
    assert back.deletes == original.deletes
    assert back.ts == original.ts
    assert back.created_at == original.created_at
    assert back.embedding == original.embedding


def test_dict_to_record_missing_key_raises(make_record) -> None:  # type: ignore[no-untyped-def]
    obj = record_to_dict(make_record())
    del obj["title"]
    with pytest.raises(KeyError):
        dict_to_record(obj)


# ---------------------------------------------------------------------------
# NDJSON serialize / parse.
# ---------------------------------------------------------------------------


def test_serialize_empty_is_empty_string() -> None:
    assert serialize_records([]) == ""


def test_serialize_records_one_per_line(make_record) -> None:  # type: ignore[no-untyped-def]
    records = [make_record(title=f"t{i}") for i in range(3)]
    text = serialize_records(records)
    assert text.endswith("\n")
    lines = [ln for ln in text.split("\n") if ln]
    assert len(lines) == 3


def test_ndjson_full_roundtrip(make_record) -> None:  # type: ignore[no-untyped-def]
    records = [make_record(title=f"t{i}") for i in range(4)]
    parsed = parse_ndjson(serialize_records(records))
    assert [r.id for r in parsed] == [r.id for r in records]


def test_parse_empty_text() -> None:
    assert parse_ndjson("") == []


def test_parse_ignores_blank_lines(make_record) -> None:  # type: ignore[no-untyped-def]
    line = record_to_line(make_record())
    text = f"\n\n{line}\n\n{line}\n\n"
    parsed = parse_ndjson(text)
    assert len(parsed) == 2


def test_parse_ignores_trailing_incomplete_line(make_record) -> None:  # type: ignore[no-untyped-def]
    good = record_to_line(make_record())
    # A trailing line without a newline that is not valid JSON is ignored.
    text = good + "\n" + '{"id": "partial'
    parsed = parse_ndjson(text)
    assert len(parsed) == 1
    assert parsed[0].id is not None


def test_parse_raises_on_bad_middle_line(make_record) -> None:  # type: ignore[no-untyped-def]
    good = record_to_line(make_record())
    # A broken line in the MIDDLE (terminated) is a real corruption -> raise.
    text = good + "\n" + "{not json}\n" + good + "\n"
    with pytest.raises(json.JSONDecodeError):
        parse_ndjson(text)


# ---------------------------------------------------------------------------
# Atomic write.
# ---------------------------------------------------------------------------


def test_atomic_write_creates_file_and_no_tmp(tmp_path: Path, make_record) -> None:  # type: ignore[no-untyped-def]
    dest = tmp_path / "sub" / "batch.ndjson"
    text = serialize_records([make_record()])
    atomic_write_text(dest, text)
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == text
    # No leftover .tmp file anywhere in the directory.
    assert list(dest.parent.glob("*.tmp")) == []


def test_atomic_write_creates_parent_dirs(tmp_path: Path) -> None:
    dest = tmp_path / "a" / "b" / "c" / "f.ndjson"
    atomic_write_text(dest, "hello")
    assert dest.read_text() == "hello"


def test_atomic_write_produces_parseable_file(tmp_path: Path, make_record) -> None:  # type: ignore[no-untyped-def]
    dest = tmp_path / "batch.ndjson"
    records = [make_record(title=f"t{i}") for i in range(2)]
    atomic_write_text(dest, serialize_records(records))
    parsed = parse_ndjson(dest.read_text(encoding="utf-8"))
    assert [r.id for r in parsed] == [r.id for r in records]


# ---------------------------------------------------------------------------
# Path helpers.
# ---------------------------------------------------------------------------


def test_batch_filename_shape() -> None:
    fid = uuid.uuid4()
    name = batch_filename(1234567890123, fid)
    assert name == f"1234567890123-{fid}.ndjson"
    assert is_ndjson_file(name)


def test_batch_filename_generates_uuid() -> None:
    a = batch_filename(1)
    b = batch_filename(1)
    assert a != b  # random uuid4 each time
    assert a.endswith(".ndjson")


def test_day_dir_layout(tmp_path: Path) -> None:
    d = day_dir(tmp_path, "bob", "2026-08-19")
    assert d == tmp_path / "bob" / "2026-08-19"


def test_author_from_path() -> None:
    assert author_from_path("bob/2026-08-19/123-abc.ndjson") == "bob"
    assert author_from_path(Path("cara") / "2026-08-19" / "f.ndjson") == "cara"


def test_author_from_empty_path_raises() -> None:
    with pytest.raises(ValueError):
        author_from_path("")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("123-abc.ndjson", True),
        ("123-abc.ndjson.tmp", False),
        ("notes.txt", False),
        ("x.ndjson", True),
    ],
)
def test_is_ndjson_file(name: str, expected: bool) -> None:
    assert is_ndjson_file(name) is expected


def test_v2_provenance_roundtrip(make_record) -> None:  # type: ignore[no-untyped-def]
    original = make_record(embedding_model="fake")
    obj = record_to_dict(original)
    assert obj["schema_version"] == 2
    assert dict_to_record(obj).embedding_model == "fake"


def test_unknown_provenance_emits_legacy_only(make_record) -> None:  # type: ignore[no-untyped-def]
    obj = record_to_dict(make_record())
    assert obj["schema_version"] == 1
    assert "embedding_model" not in obj
    assert dict_to_record(obj).embedding_model is None
    del obj["schema_version"]
    assert dict_to_record(obj).embedding_model is None


def test_legacy_extension_cannot_claim_v2_provenance(make_record) -> None:  # type: ignore[no-untyped-def]
    obj = record_to_dict(make_record())
    obj["embedding_model"] = "fake"
    assert dict_to_record(obj).embedding_model is None


@pytest.mark.parametrize("version", [0, 3, -1, True, False, "2", 2.0, None])
def test_unsupported_schema_version_rejected(make_record, version) -> None:  # type: ignore[no-untyped-def]
    obj = record_to_dict(make_record(embedding_model="fake"))
    obj["schema_version"] = version
    with pytest.raises(ValueError, match="unsupported schema_version"):
        dict_to_record(obj)


@pytest.mark.parametrize("model", [None, "", " ", 1, [], {}])
def test_v2_requires_nonempty_model(make_record, model) -> None:  # type: ignore[no-untyped-def]
    obj = record_to_dict(make_record(embedding_model="fake"))
    obj["embedding_model"] = model
    with pytest.raises(ValueError, match="v2 requires"):
        dict_to_record(obj)
    del obj["embedding_model"]
    with pytest.raises(ValueError, match="v2 requires"):
        dict_to_record(obj)


@pytest.mark.parametrize("model", ["", " "])
def test_writer_rejects_blank_model(make_record, model) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="nonempty"):
        record_to_dict(make_record(embedding_model=model))


def test_v2_unknown_optional_key_is_ignored(make_record) -> None:  # type: ignore[no-untyped-def]
    obj = record_to_dict(make_record(embedding_model="fake"))
    obj["future_optional_field"] = {"a": 1}
    assert dict_to_record(obj).embedding_model == "fake"
