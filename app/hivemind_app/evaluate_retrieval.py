"""Offline ranking regression harness; synthetic vectors do not measure model quality."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any, Literal

from .config import Identity
from .context import estimate_tokens, memory_context
from .db import create_engine, create_sessionmaker
from .embeddings import Embedder, FastEmbedEmbedder, build_embedding_text
from .repository import Repository, init_schema
from .serialization import EventRecord
from .service import MemoryService

FIXTURE = Path(__file__).with_name("retrieval_fixture.json")


def metrics(ranked: list[str], relevant: set[str], k: int = 5) -> tuple[float, float]:
    """Recall and first-relevant reciprocal rank at k; duplicate hits count once."""
    if not relevant:
        raise ValueError("relevance labels must be nonempty")
    top = ranked[:k]
    recall = len(set(top) & relevant) / len(relevant)
    rr = next((1 / rank for rank, key in enumerate(top, 1) if key in relevant), 0.0)
    return recall, rr


class FixtureEmbedder:
    """Explicit topic geometry, independent of the relevance-ID labels."""

    dim = 384

    def __init__(self, fixture: dict[str, Any]) -> None:
        topics = sorted({m["topic"] for m in fixture["memories"]})
        self.vectors: dict[str, list[float]] = {}
        for row in [*fixture["memories"], *fixture["queries"]]:
            text = row.get("text") or build_embedding_text(row["title"], row["body"])
            vector = [0.0] * self.dim
            vector[topics.index(row["topic"])] = 1.0
            self.vectors[text] = vector

    def embed(self, text: str) -> list[float]:
        return list(self.vectors[text])


def cached_embedder(cache: Path) -> Embedder:
    """Explicit local-only FastEmbed construction; never invoke its download defaults."""
    from fastembed import TextEmbedding

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    embedder = FastEmbedEmbedder()
    embedder._model = TextEmbedding(  # existing production normalization/dimension checks
        model_name="BAAI/bge-small-en-v1.5", cache_dir=str(cache), local_files_only=True
    )
    return embedder


async def evaluate(fixture_path: Path = FIXTURE, cache: Path | None = None) -> dict[str, Any]:
    fixture = json.loads(fixture_path.read_text())
    embedder = cached_embedder(cache) if cache else FixtureEmbedder(fixture)
    with tempfile.TemporaryDirectory() as directory:
        engine = create_engine(f"sqlite+aiosqlite:///{directory}/evaluation.db")
        try:
            await init_schema(engine)
            repo = Repository(create_sessionmaker(engine))
            now = datetime(2026, 1, 1, tzinfo=UTC)
            aliases: dict[str, str] = {}
            records = []
            for row in fixture["memories"]:
                record = EventRecord(
                    id=uuid.uuid5(uuid.NAMESPACE_URL, row["id"]),
                    author="evaluator",
                    event_type=row["event_type"],
                    status="info",
                    title=row["title"],
                    body=row["body"],
                    jira_project="EVAL",
                    ts=now,
                    created_at=now,
                    embedding=embedder.embed(build_embedding_text(row["title"], row["body"])),
                )
                aliases[str(record.id)] = row["id"]
                records.append(record)
                await repo.insert_local(record)
            forbidden = set()
            for index, original in enumerate(records[:3]):
                hidden = replace(original, id=uuid.uuid5(uuid.NAMESPACE_URL, f"hidden{index}"))
                await repo.insert_local(hidden)
                forbidden.add(str(hidden.id))
                if index == 0:
                    await repo.insert_local(
                        replace(
                            original,
                            id=uuid.uuid5(uuid.NAMESPACE_URL, "tomb"),
                            event_type="tombstone",
                            deletes=hidden.id,
                        )
                    )
                elif index == 1:
                    await repo.insert_local(
                        replace(
                            original, id=uuid.uuid5(uuid.NAMESPACE_URL, "new"), supersedes=hidden.id
                        )
                    )
                else:
                    await repo.import_records([replace(hidden, body="conflicting payload")])
            service = MemoryService(
                repo, embedder, Identity(owner_handle="evaluator", roster=("evaluator",))
            )
            scores: dict[Literal["semantic", "keyword", "hybrid"], list[tuple[float, float]]] = {
                "semantic": [],
                "keyword": [],
                "hybrid": [],
            }
            exact = []
            rescue_scores: dict[str, dict[str, float]] = {}
            coverage = []
            compliant = []
            leaks = 0
            for query in fixture["queries"]:
                relevant = set(query["relevant"])
                for mode in scores:
                    hits = await service.ranked_search(query["text"], top_k=5, mode=mode)
                    ids = [str(h.record.id) for h in hits]
                    leaks += len(set(ids) & forbidden)
                    score = metrics([aliases.get(key, key) for key in ids], relevant)
                    scores[mode].append(score)
                    if query.get("literal_rescue"):
                        rescue_scores.setdefault(query["text"], {})[mode] = score[0]
                    if mode == "hybrid" and query["exact_identifier"]:
                        exact.append(score[0])
                for budget in (256, 800, 2000):
                    pack = await memory_context(service, query["text"], token_budget=budget)
                    ids = [item["source_id"] for item in pack["items"]]
                    leaks += len(set(ids) & forbidden)
                    coverage.append(
                        metrics([aliases.get(key, key) for key in ids], relevant, len(ids))[0]
                    )
                    compliant.append(
                        estimate_tokens(pack) <= budget
                        and pack["estimated_tokens"] == estimate_tokens(pack)
                    )
            report = {
                "embedding": "cached real model"
                if cache
                else "synthetic fixture (ranking behavior only)",
                "memories": len(records),
                "queries": len(fixture["queries"]),
                "metrics": {
                    mode: {
                        "Recall@5": mean([s[0] for s in values]),
                        "MRR@5": mean([s[1] for s in values]),
                    }
                    for mode, values in scores.items()
                },
                "context_source_coverage": mean(coverage),
                "context_budget_compliance": mean(compliant),
                "hybrid_exact_identifier_recall@5": mean(exact),
                "literal_rescue_recall@5": rescue_scores,
                "visibility_leaks": leaks,
            }
            report["passed"] = mean(exact) >= 1.0 and all(compliant) and leaks == 0
            return report
        finally:
            await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cached-model", type=Path, help="Existing FastEmbed cache; local files only"
    )
    args = parser.parse_args()
    report = asyncio.run(evaluate(cache=args.cached_model))
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
