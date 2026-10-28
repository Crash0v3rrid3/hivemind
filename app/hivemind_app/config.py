"""Configuration, identity, and validators for the Hivemind app.

Settings are env-driven (the CLI generates the container's ``.env``).
Resolution order for a running container is simply: environment variables,
falling back to the documented defaults here. The CLI is responsible for the
``defaults < ~/.hivemind.conf < environment`` precedence on the host side.

Nothing in this module performs I/O or network access at import time, and the
real embedding model is never loaded here.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Enumerations (kept as frozensets for cheap membership tests).
# ---------------------------------------------------------------------------

EVENT_TYPES: frozenset[str] = frozenset(
    {
        "update",
        "decision",
        "blocker",
        "dependency",
        "handoff",
        "research",
        "context",
        "note",
        "friction",
        "ai_session",
        "tombstone",
    }
)

STATUS_VALUES: frozenset[str] = frozenset(
    {
        "open",
        "in_progress",
        "resolved",
        "done",
        "info",
    }
)

# ---------------------------------------------------------------------------
# Regexes (compiled once).
# ---------------------------------------------------------------------------

HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
JIRA_PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9]+$")
JIRA_TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


class ConfigError(ValueError):
    """Raised when configuration or identity values are invalid."""


# ---------------------------------------------------------------------------
# Validators (pure functions).
# ---------------------------------------------------------------------------


def validate_handle(handle: str) -> str:
    """Validate a member handle and return it unchanged.

    Handles are lowercase and match ``^[a-z0-9][a-z0-9._-]*$``.
    """
    if not isinstance(handle, str) or not HANDLE_RE.match(handle):
        raise ConfigError(f"invalid handle {handle!r}: must match {HANDLE_RE.pattern}")
    return handle


def validate_jira_project(project: str) -> str:
    """Validate a Jira project key (uppercase, ``^[A-Z][A-Z0-9]+$``)."""
    if not isinstance(project, str) or not JIRA_PROJECT_RE.match(project):
        raise ConfigError(f"invalid jira_project {project!r}: must match {JIRA_PROJECT_RE.pattern}")
    return project


def validate_jira_ticket(ticket: str | None) -> str | None:
    """Validate an optional Jira ticket id (``^[A-Z][A-Z0-9]+-\\d+$``)."""
    if ticket is None:
        return None
    if not isinstance(ticket, str) or not JIRA_TICKET_RE.match(ticket):
        raise ConfigError(f"invalid jira_ticket {ticket!r}: must match {JIRA_TICKET_RE.pattern}")
    return ticket


def _validate_port(port: int) -> int:
    if not isinstance(port, int) or isinstance(port, bool):
        raise ConfigError(f"invalid port {port!r}: must be an integer")
    if not (1 <= port <= 65535):
        raise ConfigError(f"invalid port {port!r}: must be in 1..65535")
    return port


def _validate_interval(name: str, value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"invalid {name} {value!r}: must be an integer")
    if value <= 0:
        raise ConfigError(f"invalid {name} {value!r}: must be > 0")
    return value


def _parse_roster(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated roster string into validated handles."""
    handles = [h.strip() for h in raw.split(",") if h.strip()]
    return tuple(validate_handle(h) for h in handles)


# ---------------------------------------------------------------------------
# Identity.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """The node's authorship identity.

    ``owner_handle`` is the single source of truth for who authored local
    writes. It is derived from configuration and NEVER from tool input.
    ``roster`` is the set of known members whose folders may be imported.
    """

    owner_handle: str
    roster: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        validate_handle(self.owner_handle)
        for h in self.roster:
            validate_handle(h)

    def is_member(self, handle: str) -> bool:
        """Return True if ``handle`` is in the roster (or roster is empty)."""
        if not self.roster:
            return True
        return handle in self.roster


# ---------------------------------------------------------------------------
# Settings.
# ---------------------------------------------------------------------------

DEFAULT_DATABASE_URL = "postgresql+asyncpg://hivemind:hivemind@postgres:5432/hivemind"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_EMBEDDING_DIM = 384
DEFAULT_EXPORT_INTERVAL_SEC = 60
DEFAULT_IMPORT_INTERVAL_SEC = 60
DEFAULT_MCP_HOST = "0.0.0.0"
DEFAULT_MCP_PORT = 7777
DEFAULT_HIVEMIND_PATH = "/mnt/hivemind"


@dataclass(frozen=True)
class Settings:
    """Fully resolved application settings.

    Construct via :meth:`from_env`. All fields are validated.
    """

    owner_handle: str
    roster: tuple[str, ...]
    hivemind_path: str
    database_url: str
    embedding_model: str
    embedding_dim: int
    export_interval_sec: int
    import_interval_sec: int
    mcp_host: str
    mcp_port: int
    hive_id: str = ""

    def __post_init__(self) -> None:
        validate_handle(self.owner_handle)
        for h in self.roster:
            validate_handle(h)
        _validate_port(self.mcp_port)
        _validate_interval("export_interval_sec", self.export_interval_sec)
        _validate_interval("import_interval_sec", self.import_interval_sec)
        if self.embedding_dim != DEFAULT_EMBEDDING_DIM:
            raise ConfigError(
                f"invalid embedding_dim {self.embedding_dim!r}: "
                f"must equal schema dimension {DEFAULT_EMBEDDING_DIM}"
            )

    @property
    def identity(self) -> Identity:
        """The :class:`Identity` derived from these settings."""
        return Identity(owner_handle=self.owner_handle, roster=self.roster)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        """Build settings from an environment mapping (defaults to ``os.environ``).

        Required: ``OWNER_HANDLE``. Everything else has a documented default.
        """
        e = os.environ if env is None else env

        owner = e.get("OWNER_HANDLE")
        if not owner:
            raise ConfigError("OWNER_HANDLE is required")

        roster_raw = e.get("ROSTER", "")
        roster = _parse_roster(roster_raw) if roster_raw else ()

        return cls(
            hive_id=e.get(
                "HIVEMIND_ID", str(Path(e.get("HIVEMIND_PATH", DEFAULT_HIVEMIND_PATH)).resolve())
            ),
            owner_handle=validate_handle(owner),
            roster=roster,
            hivemind_path=e.get("HIVEMIND_PATH", DEFAULT_HIVEMIND_PATH),
            database_url=e.get("DATABASE_URL", DEFAULT_DATABASE_URL),
            embedding_model=e.get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
            embedding_dim=_env_int(e, "EMBEDDING_DIM", DEFAULT_EMBEDDING_DIM),
            export_interval_sec=_env_int(e, "EXPORT_INTERVAL_SEC", DEFAULT_EXPORT_INTERVAL_SEC),
            import_interval_sec=_env_int(e, "IMPORT_INTERVAL_SEC", DEFAULT_IMPORT_INTERVAL_SEC),
            mcp_host=e.get("MCP_HOST", DEFAULT_MCP_HOST),
            mcp_port=_env_int(e, "MCP_PORT", DEFAULT_MCP_PORT),
        )


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid integer for {key}: {raw!r}") from exc


def resolve_roster(identity: Identity, root: Path) -> Identity:
    """Explicit roster wins; otherwise use the administrator roster when present.

    An invalid roster fails closed. In its absence valid folder handles are accepted.
    """
    if identity.roster:
        return identity
    path = root / "roster.json"
    if not path.exists():
        return identity
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ConfigError("roster.json must be a nonempty array of handles")
    return Identity(identity.owner_handle, tuple(validate_handle(h) for h in data))
