"""Embedding backends.

Defines the :class:`Embedder` protocol plus two implementations:

* :class:`SentenceTransformerEmbedder` — wraps ``BAAI/bge-small-en-v1.5``; the
  heavy model is **lazy-loaded** on first use so importing this module (or the
  whole package) never triggers a model download.
* :class:`FakeEmbedder` — deterministic, hash-derived vectors with no network
  or disk access, used throughout the unit tests.

Both produce L2-normalized ``list[float]`` of the configured dimension.
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Protocol, runtime_checkable

from .config import DEFAULT_EMBEDDING_DIM, DEFAULT_EMBEDDING_MODEL


def build_embedding_text(title: str, body: str) -> str:
    """The exact text embedded for a record: ``title + "\\n\\n" + body``."""
    return f"{title}\n\n{body}"


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


@runtime_checkable
class Embedder(Protocol):
    """A text -> vector embedder."""

    @property
    def dim(self) -> int:
        """The output vector dimension."""
        ...

    def embed(self, text: str) -> list[float]:
        """Embed ``text`` into an L2-normalized vector of length ``dim``."""
        ...


class SentenceTransformerEmbedder:
    """Real embedder backed by sentence-transformers.

    The model is loaded lazily on the first call to :meth:`embed` (or
    :meth:`load`), never at import time.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        dim: int = DEFAULT_EMBEDDING_DIM,
    ) -> None:
        self._model_name = model_name
        self._dim = dim
        self._model: object | None = None

    @property
    def dim(self) -> int:
        return self._dim

    def load(self) -> None:
        """Load the underlying model if not already loaded."""
        if self._model is None:
            # Imported here (not at module top) so importing this module never
            # pulls in torch / sentence-transformers or downloads weights.
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)

    def embed(self, text: str) -> list[float]:
        self.load()
        assert self._model is not None  # for type-checkers
        vec = self._model.encode(  # type: ignore[attr-defined]
            text,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        result = [float(x) for x in vec.tolist()]
        if len(result) != self._dim:
            raise ValueError(f"model produced dim {len(result)}, expected {self._dim}")
        return result


class FakeEmbedder:
    """Deterministic embedder for tests — no downloads, no network.

    Vectors are derived by hashing the input text with a rolling counter so the
    output is stable across processes and fully reproducible, then
    L2-normalized. A zero-length text still yields a valid (normalized) vector.
    """

    def __init__(self, dim: int = DEFAULT_EMBEDDING_DIM) -> None:
        if dim <= 0:
            raise ValueError("dim must be > 0")
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, text: str) -> list[float]:
        raw = text.encode("utf-8")
        floats: list[float] = []
        counter = 0
        while len(floats) < self._dim:
            digest = hashlib.sha256(raw + counter.to_bytes(4, "big")).digest()
            # 32-byte digest -> 8 float32 values.
            for i in range(0, 32, 4):
                if len(floats) >= self._dim:
                    break
                (val,) = struct.unpack(">I", digest[i : i + 4])
                # Map uint32 into [-1, 1).
                floats.append((val / 2**31) - 1.0)
            counter += 1
        return _l2_normalize(floats)


def zero_vector(dim: int = DEFAULT_EMBEDDING_DIM) -> list[float]:
    """A zero vector of length ``dim`` (used for tombstone embeddings)."""
    return [0.0] * dim
