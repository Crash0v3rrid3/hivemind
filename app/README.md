# Hivemind app

The Python container app for [Hivemind](../README.md): an MCP server plus two
background loops (export/import) that sync immutable NDJSON files through a
shared Google Drive folder. Each node holds the whole team's memory in its own
local Postgres (Postgres 16 + pgvector).

## Layout

| Module | Responsibility |
|--------|----------------|
| `config.py` | Env-driven settings, `Identity`, validators (handle / jira regexes) |
| `db.py` | Async engine + sessionmaker; registers the pgvector codec on connect |
| `models.py` | SQLAlchemy 2.0 models: `local_events`, `team_events`, `ingested_files` |
| `schemas.py` | Pydantic v2 request/response models (no identity fields; author rejected) |
| `embeddings.py` | `Embedder` protocol + lazy `FastEmbedEmbedder` + `FakeEmbedder` |
| `serialization.py` | NDJSON (de)serialize, tolerant parse, atomic write, day bucketing |
| `repository.py` | Async append-only CRUD, hidden-id + history computation, schema init |
| `service.py` | `MemoryService`: add/update/delete/get/list/search/history/daily_summary |
| `loops.py` | `export_once` / `import_once` (+ async schedulers), fully injectable |
| `mcp_server.py` | FastMCP tools + FastAPI mount + lifespan (schema init, start loops) |

## Key invariants

- **Authorship is server-enforced.** Local writes are authored as `OWNER_HANDLE`
  from config, never from tool input; `author` is rejected even inside
  `metadata`. Imported records take their author from the read-only Drive folder
  (roster-validated).
- **Append-only.** No row is mutated: updates create a new row with
  `supersedes`, deletes emit a `tombstone` with `deletes`.
- **The embedding model never loads at import time** — it is lazy-loaded on first
  use by `SentenceTransformerEmbedder`.

## Run

The image is normally launched by the CLI via Docker Compose. Standalone:

```bash
export OWNER_HANDLE=alice
export ROSTER=alice,bob,cara
export DATABASE_URL='postgresql+asyncpg://hivemind:hivemind@localhost:5432/hivemind'
export HIVEMIND_PATH=/mnt/hivemind
python -m hivemind_app
```

The MCP endpoint is served (streamable HTTP) at `http://MCP_HOST:MCP_PORT/mcp`
(defaults `0.0.0.0:7777`). A `GET /healthz` identity/readiness probe is also exposed. Docker publishes only
on loopback; do not expose this unauthenticated service to a network.

Existing databases need the [explicit migration](../docs/review-remediation.md)
before startup. Back up pending writes before upgrading.

## Test

```bash
pip install -e '.[dev]'

# Unit layer — no Docker, no Postgres, no model downloads:
pytest

# Integration layer — needs pgvector Postgres (auto-skips when unavailable).
# The test stack lives at the repo root:
docker compose -f ../docker-compose.test.yml up -d postgres
DATABASE_URL_TEST='postgresql+asyncpg://hivemind:hivemind@localhost:5433/hivemind' \
    pytest -m integration
docker compose -f ../docker-compose.test.yml down -v
```

Lint / type-check:

```bash
ruff check . && ruff format --check .
mypy hivemind_app
```
