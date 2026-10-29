"""Bounded, process-local observations; never evidence of peer delivery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

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


# 40 samples across both cycles fit comfortably within the CLI's 64 KiB cap,
# even with JSON escaping. Percent encoding keeps paths ASCII and reversible.
MAX_PROBLEM_PATH_BYTES = 512


@dataclass
class _CycleState:
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    last_failure_category: str | None = None
    problem_count: int = 0
    problems: list[dict[str, str | None]] = field(default_factory=list)
    categories: set[str] = field(default_factory=set)

    def snapshot(self) -> dict[str, Any]:
        return {
            "last_attempt_at": self.last_attempt_at,
            "last_success_at": self.last_success_at,
            "last_failure_category": self.last_failure_category,
            "problem_count": self.problem_count,
            "problems": [dict(problem) for problem in self.problems],
        }


class SyncStatus:
    def __init__(self, loops_enabled: bool, clock: Callable[[], datetime] | None = None):
        self.clock = clock or (lambda: datetime.now(UTC))
        self.telemetry_started_at = self.now()
        self.loops_enabled = loops_enabled
        self._cycles = {name: _CycleState() for name in ("import", "export")}

    @property
    def cycles(self) -> dict[str, dict[str, Any]]:
        """Detached observations; callers cannot mutate the cycle state."""
        return {name: cycle.snapshot() for name, cycle in self._cycles.items()}

    def now(self) -> str:
        return self.clock().astimezone(UTC).isoformat()

    def begin(self, direction: str) -> None:
        cycle = self._cycles[direction]
        cycle.last_attempt_at = self.now()
        cycle.problem_count = 0
        cycle.problems.clear()
        cycle.categories.clear()

    def problem(self, direction: str, reason: str, path: str | None = None) -> None:
        cycle = self._cycles[direction]
        if reason in GUIDANCE:
            cycle.categories.add(reason)
        cycle.last_failure_category = reason
        cycle.problem_count += 1
        if len(cycle.problems) >= 20:
            return
        if path is not None:
            # surrogateescape round-trips non-UTF-8 POSIX filename bytes too.
            path = quote(path, safe="/", errors="surrogateescape")
            if len(path) > MAX_PROBLEM_PATH_BYTES:
                return  # Omit the sample, never its count/category or health.
        cycle.problems.append({"path": path, "reason": reason})

    def fail_if_unreported(self, direction: str, reason: str) -> None:
        if not self._cycles[direction].problem_count:
            self.problem(direction, reason)

    def finish(self, direction: str) -> None:
        cycle = self._cycles[direction]
        if not cycle.problem_count:
            cycle.last_success_at = self.now()
            cycle.last_failure_category = None

    async def snapshot(self, repo: Any) -> dict[str, Any]:
        pending, conflicts = await repo.sync_counts()
        reasons = set().union(*(cycle.categories for cycle in self._cycles.values()))
        reasons.update(
            c.last_failure_category
            for c in self._cycles.values()
            if c.last_failure_category in GUIDANCE
        )
        if conflicts:
            reasons.add("conflicts")
        completed = all(c.last_success_at is not None for c in self._cycles.values())
        degraded = bool(reasons) or any(
            c.last_failure_category is not None for c in self._cycles.values()
        )
        return {
            "service": "hivemind",
            "health": "degraded" if degraded else ("ok" if completed else "unknown"),
            "telemetry_started_at": self.telemetry_started_at,
            "loops_enabled": self.loops_enabled,
            "remote_delivery": "unknown",
            "pending_local_exports": pending,
            "quarantined_ids": conflicts,
            **self.cycles,
            "recovery_guidance": [GUIDANCE[r] for r in sorted(reasons)],
        }
