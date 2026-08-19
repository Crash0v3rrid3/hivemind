"""Entrypoint: ``python -m hivemind_app`` starts the MCP server + loops."""

from __future__ import annotations

from .mcp_server import run

if __name__ == "__main__":
    run()
