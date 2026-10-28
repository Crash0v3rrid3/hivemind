"""Tests for config resolution, identity, and validators (SPEC §10, §14)."""

from __future__ import annotations

import pytest

from hivemind_app.config import (
    DEFAULT_DATABASE_URL,
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EXPORT_INTERVAL_SEC,
    DEFAULT_HIVEMIND_PATH,
    DEFAULT_IMPORT_INTERVAL_SEC,
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PORT,
    ConfigError,
    Identity,
    Settings,
    validate_handle,
    validate_jira_project,
    validate_jira_ticket,
)

# ---------------------------------------------------------------------------
# Handle regex.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("handle", ["alice", "bob99", "a", "a.b_c-d", "0user", "x1"])
def test_validate_handle_accepts_valid(handle: str) -> None:
    assert validate_handle(handle) == handle


@pytest.mark.parametrize(
    "handle",
    ["Alice", "-bad", ".dot", "with space", "UPPER", "b@d", "", "café"],
)
def test_validate_handle_rejects_invalid(handle: str) -> None:
    with pytest.raises(ConfigError):
        validate_handle(handle)


# ---------------------------------------------------------------------------
# Jira project / ticket regex.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("project", ["BETEL", "AB", "PROJ1", "X9Y"])
def test_validate_jira_project_accepts_valid(project: str) -> None:
    assert validate_jira_project(project) == project


@pytest.mark.parametrize(
    "project",
    ["betel", "A", "1AB", "AB-", "A B", "", "Ab", "PROJ_1"],
)
def test_validate_jira_project_rejects_invalid(project: str) -> None:
    with pytest.raises(ConfigError):
        validate_jira_project(project)


@pytest.mark.parametrize("ticket", ["BETEL-42", "AB-1", "PROJ1-999"])
def test_validate_jira_ticket_accepts_valid(ticket: str) -> None:
    assert validate_jira_ticket(ticket) == ticket


def test_validate_jira_ticket_allows_none() -> None:
    assert validate_jira_ticket(None) is None


@pytest.mark.parametrize(
    "ticket",
    ["BETEL", "betel-1", "BETEL-", "BETEL-1a", "A-1", "BETEL_1", "-1"],
)
def test_validate_jira_ticket_rejects_invalid(ticket: str) -> None:
    with pytest.raises(ConfigError):
        validate_jira_ticket(ticket)


# ---------------------------------------------------------------------------
# Identity.
# ---------------------------------------------------------------------------


def test_identity_membership_with_roster() -> None:
    ident = Identity(owner_handle="alice", roster=("alice", "bob"))
    assert ident.is_member("bob") is True
    assert ident.is_member("cara") is False


def test_identity_empty_roster_allows_all() -> None:
    ident = Identity(owner_handle="alice", roster=())
    assert ident.is_member("anyone") is True


def test_identity_rejects_bad_owner() -> None:
    with pytest.raises(ConfigError):
        Identity(owner_handle="Alice")


def test_identity_rejects_bad_roster_member() -> None:
    with pytest.raises(ConfigError):
        Identity(owner_handle="alice", roster=("alice", "BadHandle"))


# ---------------------------------------------------------------------------
# Settings.from_env — defaults, overrides, precedence, errors.
# ---------------------------------------------------------------------------


def test_settings_defaults() -> None:
    s = Settings.from_env({"OWNER_HANDLE": "alice"})
    assert s.owner_handle == "alice"
    assert s.roster == ()
    assert s.hivemind_path == DEFAULT_HIVEMIND_PATH
    assert s.database_url == DEFAULT_DATABASE_URL
    assert s.embedding_model == DEFAULT_EMBEDDING_MODEL
    assert s.embedding_dim == DEFAULT_EMBEDDING_DIM
    assert s.export_interval_sec == DEFAULT_EXPORT_INTERVAL_SEC
    assert s.import_interval_sec == DEFAULT_IMPORT_INTERVAL_SEC
    assert s.mcp_host == DEFAULT_MCP_HOST
    assert s.mcp_port == DEFAULT_MCP_PORT


def test_settings_environment_overrides_win() -> None:
    env = {
        "OWNER_HANDLE": "bob",
        "ROSTER": "alice,bob, cara ",
        "HIVEMIND_PATH": "/custom/mount",
        "DATABASE_URL": "postgresql+asyncpg://u:p@db:5432/x",
        "EMBEDDING_MODEL": "some/model",
        "EMBEDDING_DIM": "384",
        "EXPORT_INTERVAL_SEC": "5",
        "IMPORT_INTERVAL_SEC": "7",
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": "9999",
    }
    s = Settings.from_env(env)
    assert s.owner_handle == "bob"
    assert s.roster == ("alice", "bob", "cara")  # trimmed + parsed
    assert s.hivemind_path == "/custom/mount"
    assert s.database_url == "postgresql+asyncpg://u:p@db:5432/x"
    assert s.embedding_model == "some/model"
    assert s.embedding_dim == 384
    assert s.export_interval_sec == 5
    assert s.import_interval_sec == 7
    assert s.mcp_host == "127.0.0.1"
    assert s.mcp_port == 9999


def test_settings_identity_property() -> None:
    s = Settings.from_env({"OWNER_HANDLE": "alice", "ROSTER": "alice,bob"})
    ident = s.identity
    assert ident.owner_handle == "alice"
    assert ident.roster == ("alice", "bob")


def test_settings_missing_owner_raises() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({})


def test_settings_empty_owner_raises() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"OWNER_HANDLE": ""})


def test_settings_bad_owner_handle_raises() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"OWNER_HANDLE": "Alice"})


def test_settings_bad_roster_member_raises() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"OWNER_HANDLE": "alice", "ROSTER": "alice,BAD"})


@pytest.mark.parametrize("port", ["0", "65536", "-1", "70000"])
def test_settings_bad_port_raises(port: str) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"OWNER_HANDLE": "alice", "MCP_PORT": port})


def test_settings_non_integer_port_raises() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"OWNER_HANDLE": "alice", "MCP_PORT": "abc"})


@pytest.mark.parametrize("key", ["EXPORT_INTERVAL_SEC", "IMPORT_INTERVAL_SEC"])
def test_settings_non_positive_interval_raises(key: str) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"OWNER_HANDLE": "alice", key: "0"})


def test_settings_non_integer_interval_raises() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"OWNER_HANDLE": "alice", "EXPORT_INTERVAL_SEC": "soon"})


def test_settings_bad_embedding_dim_raises() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"OWNER_HANDLE": "alice", "EMBEDDING_DIM": "0"})


def test_settings_blank_int_env_falls_back_to_default() -> None:
    s = Settings.from_env({"OWNER_HANDLE": "alice", "MCP_PORT": ""})
    assert s.mcp_port == DEFAULT_MCP_PORT


def test_settings_is_frozen() -> None:
    s = Settings.from_env({"OWNER_HANDLE": "alice"})
    with pytest.raises((AttributeError, TypeError)):
        s.owner_handle = "bob"  # type: ignore[misc]
