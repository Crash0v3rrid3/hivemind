"""Portable lexical term matching (not BM25) and reciprocal rank fusion."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Literal

from .serialization import EventRecord

SearchMode = Literal["semantic", "keyword", "hybrid"]
MAX_QUERY_TERMS = 32
RRF_CONSTANT = 60


def query_terms(query: str) -> list[str]:
    """Bounded, ordered terms retaining identifiers, paths and literal SQL punctuation."""
    normalized = query.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )
    tokens = re.findall(r"[\w%\\]+(?:[./:\\%+@#-][\w%\\]+)*", normalized)
    return list(dict.fromkeys(tokens))[:MAX_QUERY_TERMS]


def like_pattern(term: str) -> str:
    """Escape LIKE syntax; only the surrounding substring wildcards are syntax."""
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@dataclass(frozen=True)
class RankedHit:
    record: EventRecord
    score: float
    channels: tuple[str, ...]


def reciprocal_rank_fusion(channels: dict[str, list[EventRecord]]) -> list[RankedHit]:
    """Equal channel weights, one contribution per ID per channel, stable ID ties."""
    records: dict[uuid.UUID, EventRecord] = {}
    scores: dict[uuid.UUID, float] = {}
    matched: dict[uuid.UUID, list[str]] = {}
    for channel, candidates in channels.items():
        seen: set[uuid.UUID] = set()
        for record in candidates:
            if record.id in seen:
                continue
            seen.add(record.id)
            records.setdefault(record.id, record)
            scores[record.id] = scores.get(record.id, 0.0) + 1 / (RRF_CONSTANT + len(seen))
            matched.setdefault(record.id, []).append(channel)
    return [
        RankedHit(records[key], scores[key], tuple(matched[key]))
        for key in sorted(records, key=lambda key: (-scores[key], str(key)))
    ]
