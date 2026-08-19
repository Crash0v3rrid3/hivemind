"""Tests for the embedder protocol, FakeEmbedder, and helpers (SPEC §8, §14).

The real SentenceTransformerEmbedder is verified only for its lazy-loading
contract (no model download at import/construction time) so the unit layer never
touches the network.
"""

from __future__ import annotations

import math

import pytest

from hivemind_app.config import DEFAULT_EMBEDDING_DIM
from hivemind_app.embeddings import (
    FAKE_EMBEDDING_MODEL,
    Embedder,
    FakeEmbedder,
    SentenceTransformerEmbedder,
    build_embedding_text,
    resolve_embedder,
    zero_vector,
)


def _l2(vec: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vec))


# ---------------------------------------------------------------------------
# build_embedding_text.
# ---------------------------------------------------------------------------


def test_build_embedding_text_format() -> None:
    assert build_embedding_text("Title", "Body") == "Title\n\nBody"


def test_build_embedding_text_empty() -> None:
    assert build_embedding_text("", "") == "\n\n"


# ---------------------------------------------------------------------------
# FakeEmbedder — determinism, dimension, normalization.
# ---------------------------------------------------------------------------


def test_fake_embedder_dim() -> None:
    assert FakeEmbedder(dim=16).dim == 16
    assert FakeEmbedder().dim == DEFAULT_EMBEDDING_DIM


def test_fake_embedder_output_length() -> None:
    emb = FakeEmbedder(dim=384)
    assert len(emb.embed("some text")) == 384


@pytest.mark.parametrize("dim", [1, 7, 8, 16, 384])
def test_fake_embedder_length_matches_dim(dim: int) -> None:
    assert len(FakeEmbedder(dim=dim).embed("hello")) == dim


def test_fake_embedder_deterministic_same_process() -> None:
    emb = FakeEmbedder(dim=32)
    assert emb.embed("repeatable") == emb.embed("repeatable")


def test_fake_embedder_stable_across_instances() -> None:
    a = FakeEmbedder(dim=32).embed("stable")
    b = FakeEmbedder(dim=32).embed("stable")
    assert a == b  # derived from a hash — reproducible across instances/processes


def test_fake_embedder_different_text_differs() -> None:
    emb = FakeEmbedder(dim=32)
    assert emb.embed("alpha") != emb.embed("beta")


def test_fake_embedder_is_l2_normalized() -> None:
    vec = FakeEmbedder(dim=64).embed("normalize me")
    assert _l2(vec) == pytest.approx(1.0, abs=1e-9)


def test_fake_embedder_empty_text_is_valid_normalized() -> None:
    vec = FakeEmbedder(dim=16).embed("")
    assert len(vec) == 16
    assert _l2(vec) == pytest.approx(1.0, abs=1e-9)


def test_fake_embedder_rejects_bad_dim() -> None:
    with pytest.raises(ValueError):
        FakeEmbedder(dim=0)
    with pytest.raises(ValueError):
        FakeEmbedder(dim=-3)


def test_fake_embedder_satisfies_protocol() -> None:
    emb = FakeEmbedder(dim=8)
    assert isinstance(emb, Embedder)


# ---------------------------------------------------------------------------
# zero_vector.
# ---------------------------------------------------------------------------


def test_zero_vector_default_dim() -> None:
    z = zero_vector()
    assert len(z) == DEFAULT_EMBEDDING_DIM
    assert all(x == 0.0 for x in z)


def test_zero_vector_custom_dim() -> None:
    assert zero_vector(5) == [0.0, 0.0, 0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# SentenceTransformerEmbedder — lazy loading contract only.
# ---------------------------------------------------------------------------


def test_sentence_transformer_constructs_without_loading() -> None:
    # Constructing must NOT import torch / sentence-transformers or download.
    emb = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5", dim=384)
    assert emb.dim == 384
    assert emb._model is None  # not yet loaded


def test_sentence_transformer_satisfies_protocol() -> None:
    emb = SentenceTransformerEmbedder(dim=384)
    assert isinstance(emb, Embedder)


# ---------------------------------------------------------------------------
# resolve_embedder — selection logic (SPEC §8; enables the test compose).
# ---------------------------------------------------------------------------


def test_resolve_embedder_fake_sentinel_selects_fake() -> None:
    emb = resolve_embedder(FAKE_EMBEDDING_MODEL, 384)
    assert isinstance(emb, FakeEmbedder)
    assert emb.dim == 384


def test_resolve_embedder_real_model_is_lazy_not_loaded() -> None:
    emb = resolve_embedder("BAAI/bge-small-en-v1.5", 384)
    assert isinstance(emb, SentenceTransformerEmbedder)
    assert emb._model is None  # constructing must not load torch/download


def test_resolve_embedder_explicit_instance_wins() -> None:
    injected = FakeEmbedder(dim=8)
    assert resolve_embedder("BAAI/bge-small-en-v1.5", 384, injected) is injected
