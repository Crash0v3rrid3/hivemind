"""Bounded, process-local observations; never evidence of peer delivery."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

GUIDANCE = {
    "missing_mount": "Restore the configured hive mount and provisioned owner folder; retry.",
    "roster": "Check administrator roster readability and valid member handles; retry.",
    "read": "Check member folder permissions and availability; retry.",
    "parse": "Check the published NDJSON format with its publisher; retry after repair.",
    "incomplete": "Wait for publication to complete, then retry.",
    "import": "Check database availability and identity/model compatibility; retry.",
    "export": "Check local publication permissions and database availability; retry.",
    "conflicts": "Inspect conflicting UUID evidence with the publisher; preserve evidence.",
}


class SyncStatus:
    def __init__(self, loops_enabled: bool, clock: Callable[[], datetime] | None = None):
        self.clock = clock or (lambda: datetime.now(UTC))
        self.telemetry_started_at = self.now()
        self.loops_enabled = loops_enabled
        self.cycles: dict[str, dict[str, Any]] = {
            name: {
                "last_attempt_at": None,
                "last_success_at": None,
                "last_failure_category": None,
                "problem_count": 0,
                "problems": [],
            }
            for name in ("import", "export")
        }

        self._categories: dict[str, set[str]] = {name: set() for name in self.cycles}

    def now(self) -> str:
        return self.clock().astimezone(UTC).isoformat()

    def begin(self, direction: str) -> None:
        cycle = self.cycles[direction]
        cycle.update(last_attempt_at=self.now(), problem_count=0, problems=[])
        self._categories[direction].clear()

    def problem(self, direction: str, reason: str, path: str | None = None) -> None:
        cycle = self.cycles[direction]
        if reason in GUIDANCE:
            self._categories[direction].add(reason)
        cycle["last_failure_category"] = reason
        cycle["problem_count"] += 1
        if len(cycle["problems"]) < 20:
            cycle["problems"].append({"path": path, "reason": reason})

    def finish(self, direction: str) -> None:
        cycle = self.cycles[direction]
        if not cycle["problem_count"]:
            cycle["last_success_at"] = self.now()
            cycle["last_failure_category"] = None

    async def snapshot(self, repo: Any) -> dict[str, Any]:
        pending, conflicts = await repo.sync_counts()
        reasons = set().union(*self._categories.values())
        reasons.update(
            c["last_failure_category"]
            for c in self.cycles.values()
            if c["last_failure_category"] in GUIDANCE
        )
        if conflicts:
            reasons.add("conflicts")
        completed = all(c["last_success_at"] is not None for c in self.cycles.values())
        degraded = bool(reasons) or any(
            c["last_failure_category"] is not None for c in self.cycles.values()
        )
        return {
            "service": "hivemind",
            "health": "degraded" if degraded else ("ok" if completed else "unknown"),
            "telemetry_started_at": self.telemetry_started_at,
            "loops_enabled": self.loops_enabled,
            "remote_delivery": "unknown",
            "pending_local_exports": pending,
            "quarantined_ids": conflicts,
            **{
                name: {**cycle, "problems": list(cycle["problems"])}
                for name, cycle in self.cycles.items()
            },
            "recovery_guidance": [GUIDANCE[r] for r in sorted(reasons)],
        }
