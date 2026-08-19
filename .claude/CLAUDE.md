# Hivemind — repo guide for Claude

Hivemind is a **serverless, append-only, semantically-searchable shared team memory exposed over
MCP**. Every developer runs an identical local Docker Compose stack; nodes sync by exchanging
**immutable NDJSON files** through one shared **Google Drive** folder (`hivemind/`). Each node holds
the whole team's memory in its own local Postgres.

**[SPEC.md](../SPEC.md) is the single source of truth.** This file is a map, not a replacement —
when in doubt, read the spec. Build exactly to it.

## Architecture (one node)

Per-node Compose services:

- **gdrive** — rclone FUSE-mounts `hivemind/` using *this developer's own* Drive credentials.
- **postgres** — Postgres 16 + pgvector, on a **LOCAL** named volume (never on the mount).
- **app** — FastMCP server (streamable HTTP at `MCP_HOST:MCP_PORT/mcp`) plus two background loops:
  - **export loop** — writes this node's un-exported `local_events` to new immutable NDJSON batch
    files under `hivemind/<OWNER_HANDLE>/<day>/`.
  - **import loop** — reads every roster member's folder, stamps `author` from the folder name, and
    inserts into `team_events` with `ON CONFLICT (id) DO NOTHING`.

Nodes never talk directly — they exchange files via the shared folder. Trust comes from the
filesystem: each Drive folder is read-write for its owner, read-only for everyone else, and each
node's mount authenticates as its own developer, so **authorship is derived from which folder a
record sits in, never from a field in the file.**

## Repo layout

```
app/    Python container app (MCP server + loops).  Owns: models, schemas, serialization,
        repository (append-only CRUD), service (add/update/delete/get/list/search/history/
        daily_summary), embeddings, loops, mcp_server. Tests in app/tests/.
cli/    Node/TS host CLI, published as @browserstack/hivemind. Provisions the node: config,
        rclone mount, generate .env/rclone.conf/docker-compose.yml, wire Claude (CLAUDE.md
        block, MCP entry, guard hook), up/down/status/doctor. Tests in cli/test/.
.github/ CI (make check on PRs) + release (npm → GitHub Packages, image → GHCR) on tag.
.claude/ This guide + subagents.
Makefile / README.md / SPEC.md / LICENSE
```

## Invariants (SPEC §14 — must always hold; tests assert them)

1. **Author is server-controlled.** Local writes are authored as `OWNER_HANDLE` from config,
   **never** from client/tool input — reject any attempt to pass `author`, including inside
   `metadata`. Imported records take `author` from the read-only folder name, validated against the
   roster.
2. **Append-only files.** NDJSON batch files are write-once (atomic `.tmp` → `os.rename`); never
   modified or deleted. Updates and deletes are *new* records — an update sets `supersedes=<old_id>`,
   a delete emits an `event_type="tombstone"` record with `deletes=<target_id>` and a zero vector.
   Updates/deletes are allowed on **own records only**.
3. **Local DB only.** The only shared artifacts are NDJSON files on the mount; Postgres data stays
   on a local volume.
4. **Eventual consistency.** Visibility lags by roughly `export interval + Drive sync + import
   interval`.
5. **No secrets in records.** Ever.

Derived rules to keep in mind while coding:
- Visible state: `hidden_ids` = {non-null `supersedes`} ∪ {non-null `deletes`} across *both* tables;
  a record is visible iff its id ∉ `hidden_ids` and its own `event_type != 'tombstone'`.
- Embeddings: `BAAI/bge-small-en-v1.5`, 384-dim, L2-normalized, cosine (`<=>`), embedded text =
  `title + "\n\n" + body`, computed at write time and **carried in files** so importers never
  re-embed. The real model must not load at import time — use `FakeEmbedder` in all unit tests.
- Search: embed the query, per-table `ORDER BY embedding <=> :q LIMIT pool` with
  `pool = max(top_k, 200)`, union candidates, hydrate, apply filters, drop hidden/tombstones,
  re-sort by distance, return `top_k`.
- Validation: `jira_project` required, uppercase, `^[A-Z][A-Z0-9]+$`; `jira_ticket` (if present)
  `^[A-Z][A-Z0-9]+-\d+$`; handle `^[a-z0-9][a-z0-9._-]*$`.

## Working in this repo

Use the root `Makefile` (all targets self-bootstrap the venv / node_modules):

```sh
make check       # lint + typecheck + UNIT tests, both packages — must be green on a clean checkout
make fmt         # ruff format (app) + prettier (cli)
make lint        # ruff (app) + eslint/prettier (cli)
make typecheck   # mypy --strict (app) + tsc (cli)
make test        # unit layer both packages (integration tests skip without Postgres)
```

- Python (`app/`): ruff (E,F,I,UP,B,C4,SIM,RUF; line-length 100), `mypy --strict`, pytest with
  `asyncio_mode=auto` and an `integration` marker. Integration tests skip when `DATABASE_URL_TEST`
  is unset or Postgres is down; `docker-compose.test.yml` spins up pgvector for local runs.
- Node (`cli/`): TypeScript strict, eslint (typescript-eslint), prettier, vitest.

## Conventions

- **Ownership boundaries matter.** `app/**` (minus `app/tests/`), `cli/**` (minus `cli/test/`),
  root+`.github`+`.claude` tooling, and the two test trees each have a distinct owner (SPEC §1).
  Stay in the relevant subtree.
- The CLI's file editors (CLAUDE.md block, `.claude.json` / `settings.json` merges, env + Compose
  generation, Drive folder-id extraction) must be **pure functions** — string/object in, out — so
  they are unit-testable without touching real home directories.
- No Alembic: schema init is idempotent (`CREATE EXTENSION IF NOT EXISTS vector`,
  `metadata.create_all`, then `CREATE INDEX IF NOT EXISTS` for the HNSW indexes).
- Do not edit `.claude/settings.json`.
