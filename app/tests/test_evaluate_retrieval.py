"""Metric arithmetic and the real SQLite evaluation regression gate."""

import pytest

from hivemind_app.evaluate_retrieval import evaluate, metrics


def test_metrics() -> None:
    assert metrics(["noise", "a", "b"], {"a", "b"}) == (1.0, 0.5)
    assert metrics(["a", "a"], {"a", "b"}) == (0.5, 1.0)
    assert metrics(["noise"] * 5 + ["a"], {"a"}) == (0.0, 0.0)
    assert metrics([], {"a"}) == (0.0, 0.0)
    with pytest.raises(ValueError):
        metrics([], set())


async def test_offline_evaluation() -> None:
    report = await evaluate()
    assert report["memories"] >= 24
    assert report["queries"] >= 12
    assert report["passed"]
    rescue = report["literal_rescue_recall@5"]["src/sync_retry.py"]
    assert rescue["semantic"] == 0.0
    assert rescue["keyword"] == rescue["hybrid"] == 1.0
    assert report["visibility_leaks"] == 0
    assert report["context_budget_compliance"] == 1.0


def test_cached_model_is_explicitly_offline(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import sys
    from types import SimpleNamespace

    from hivemind_app.evaluate_retrieval import cached_embedder

    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")
    calls = []

    def load(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        raise FileNotFoundError("missing local cache")

    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=load))
    with pytest.raises(FileNotFoundError):
        cached_embedder(tmp_path)
    assert calls == [
        {
            "model_name": "BAAI/bge-small-en-v1.5",
            "cache_dir": str(tmp_path),
            "local_files_only": True,
        }
    ]
    import os

    assert os.environ["HF_HUB_OFFLINE"] == "1"


async def test_dropping_keyword_channel_fails_exact_identifier_gate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from hivemind_app.repository import Repository

    async def no_keywords(self, query, pool, *, filters=None):  # type: ignore[no-untyped-def]
        return []

    monkeypatch.setattr(Repository, "keyword_candidates", no_keywords)
    report = await evaluate()
    assert report["literal_rescue_recall@5"]["src/sync_retry.py"]["hybrid"] == 0.0
    assert report["hybrid_exact_identifier_recall@5"] < 1.0
    assert not report["passed"]
