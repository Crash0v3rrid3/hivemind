"""Source-only context packs; byte estimates are approximate, not tokenizer guarantees."""

from __future__ import annotations

import json
import unicodedata
from typing import TYPE_CHECKING, Any

from .schemas import MAX_QUERY_LENGTH, SearchFilters

if TYPE_CHECKING:
    from .service import MemoryService

BUDGET_METHOD = "ceil(UTF-8 JSON bytes / 4); indent=2; not a tokenizer guarantee"


def estimate_tokens(pack: dict[str, Any]) -> int:
    """Count the entire JSON pack, including its estimate and envelope fields.

    Two-space indentation matches MCP's text JSON representation. Unicode is
    encoded directly (not ASCII escaped), as in the MCP serializer.
    """
    return (len(json.dumps(pack, ensure_ascii=False, indent=2).encode("utf-8")) + 3) // 4


def _measure(pack: dict[str, Any]) -> int:
    pack["estimated_tokens"] = 0
    while (size := estimate_tokens(pack)) != pack["estimated_tokens"]:
        pack["estimated_tokens"] = size
    return size


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


async def memory_context(
    service: MemoryService,
    task: str,
    jira_project: str | None = None,
    jira_ticket: str | None = None,
    token_budget: int = 2000,
    max_items: int = 12,
) -> dict[str, Any]:
    """Retrieve a bounded hybrid pack without writing or generating source claims."""
    for name, value, low, high in [
        ("token_budget", token_budget, 256, 16000),
        ("max_items", max_items, 1, 50),
    ]:
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be an integer between {low} and {high}")
    if not isinstance(task, str) or not task.strip() or len(task) > MAX_QUERY_LENGTH:
        raise ValueError(f"task must contain 1 to {MAX_QUERY_LENGTH} nonblank characters")
    pack: dict[str, Any] = {
        "task": task,
        "items": [],
        "estimated_tokens": 0,
        "token_budget": token_budget,
        "truncated": False,
        "budget_method": BUDGET_METHOD,
    }
    if _measure(pack) > token_budget:
        raise ValueError("task is too large for the context pack envelope and token_budget")
    hits = await service.ranked_search(
        task,
        top_k=200,
        filters=SearchFilters(jira_project=jira_project, jira_ticket=jira_ticket),
        mode="hybrid",
    )
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        record = hit.record
        key = (_normalized(record.title), _normalized(record.body))
        if key in seen:
            continue
        seen.add(key)
        if len(pack["items"]) >= max_items:
            pack["truncated"] = True
            break
        item = {
            "source_id": str(record.id),
            "author": record.author,
            "event_type": record.event_type,
            "status": record.status,
            "title": record.title,
            "excerpt": record.body,
            "timestamp": record.ts.isoformat(),
            "matched_channels": list(hit.channels),
            "reason": f"Retrieved via {', '.join(hit.channels)}; category: {record.event_type}",
        }
        pack["items"].append(item)
        if _measure(pack) <= token_budget:
            continue
        pack["truncated"] = True
        # Slice Python characters, never partial UTF-8 bytes. Keep only source text.
        low, high = 0, len(record.body)
        item["excerpt"] = ""
        if _measure(pack) > token_budget:
            pack["items"].pop()
            continue
        while low < high:
            mid = (low + high + 1) // 2
            item["excerpt"] = record.body[:mid]
            if _measure(pack) <= token_budget:
                low = mid
            else:
                high = mid - 1
        item["excerpt"] = record.body[:low]
    _measure(pack)
    return pack
