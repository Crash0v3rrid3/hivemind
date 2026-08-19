# Hivemind

**Serverless, append-only, semantically-searchable shared team memory — over [MCP](https://modelcontextprotocol.io).**

Hivemind gives an engineering team a single, shared, durable memory of *how work actually
happens* — decisions, blockers, friction, hand-offs, research, and AI-assist notes — that any
teammate's Claude (or other MCP client) can search semantically and append to. There is **no
central server**: every developer runs an identical local stack, and nodes sync by exchanging
**immutable NDJSON files** through one shared **Google Drive** folder. Each node keeps the whole
team's memory in its own local Postgres.

---

## Why it looks the way it does

- **Serverless & offline-first.** Nothing to operate, no shared database to protect. Each node is
  self-contained; sync is just files landing in a folder.
- **Append-only & immutable.** Records are never mutated. Updates and deletes are *new* records
  (`supersedes` / a `tombstone`), so history is always reconstructable and files are write-once.
- **Trust from the filesystem, not the payload.** Authorship is derived from *which Drive folder
  a record sits in*, never from a field in the file. Each folder is read-write for its owner and
  read-only for everyone else, and each node's mount authenticates as its own developer — so Drive
  itself rejects forged cross-author writes.
- **Local semantic search.** Embeddings (`BAAI/bge-small-en-v1.5`, 384-dim) are computed at write
  time with a local model — no API, no key — and carried in the files so importers never re-embed.

## Architecture

```
        ┌─────────────────────────── one shared Google Drive folder: hivemind/ ───────────────────────────┐
        │   hivemind/alice/2026-08-19/<epoch>-<uuid>.ndjson   (alice: rw, everyone else: ro)               │
        │   hivemind/bob/2026-08-19/<epoch>-<uuid>.ndjson     (bob:   rw, everyone else: ro)               │
        └───────────────────────────────────────────────────────────────────────────────────────────────┘
                    ▲   write own folder                         │  read all folders
                    │   (export loop)                            ▼  (import loop)
   ┌────────────────┴───────────────────────────────────────────────────────────────┐
   │  Each developer's node — identical local Docker Compose stack                     │
   │                                                                                   │
   │   gdrive  ── rclone FUSE mount of hivemind/ using THIS dev's OWN Drive creds      │
   │   postgres ─ Postgres 16 + pgvector, LOCAL volume (never on the mount)            │
   │   app     ── FastMCP server (streamable HTTP /mcp) + export loop + import loop     │
   └───────────────────────────────────────────────────────────────────────────────┘
                    ▲
                    │  MCP over HTTP (localhost:<mcp_port>/mcp)
              Claude Code / any MCP client
```

Two deployables live in this monorepo:

- **`app/`** — the Python container (MCP server + the two background sync loops). Holds the DB
  models, serialization, append-only repository, search service, and the FastMCP tools. Runs as
  the `ghcr.io/browserstack/hivemind` image.
- **`cli/`** — the Node/TS host CLI, published as `@browserstack/hivemind`. It never contains team
  data; it *provisions the node*: reads `~/.hivemind.conf`, sets up the rclone mount, generates the
  Compose stack and env, wires Claude (CLAUDE.md block, MCP entry, guard hook), and runs
  `up`/`down`/`doctor`.

The full data model, file format, loop semantics, MCP tool surface, and invariants are specified in
**[SPEC.md](./SPEC.md)** — the single source of truth for the implementation.

---

## Quickstart (via `npx`)

The CLI is published privately to GitHub Packages, so configure npm for the `@browserstack` scope
once, then drive everything through `npx`.

```sh
# 1. Point the @browserstack scope at GitHub Packages and authenticate (needs a GitHub token
#    with read:packages), e.g. in ~/.npmrc:
#      @browserstack:registry=https://npm.pkg.github.com
#      //npm.pkg.github.com/:_authToken=${GITHUB_TOKEN}

# 2. Scaffold your config (~/.hivemind.conf), then fill in the shared Drive folder URL + your handle
npx -y @browserstack/hivemind init
npx -y @browserstack/hivemind config --edit

# 3. One-shot: rclone mount + generate the stack, bring it up, verify, and wire Claude
npx -y @browserstack/hivemind install

# First boot pulls the app image and warms the embedding model (~60–90s).
npx -y @browserstack/hivemind status
npx -y @browserstack/hivemind doctor
```

`~/.hivemind.conf` (TOML) is the only file you edit by hand:

```toml
gdrive_folder_url = "https://drive.google.com/drive/folders/1AbCd...Xyz"
username          = "alice"
mcp_port          = 7777
```

Everything else — the container `.env`, `rclone.conf`, and `docker-compose.yml` — is generated into
`~/.hivemind/` from that config. **Secrets never go in `~/.hivemind.conf`**; the Drive OAuth token
lives in `~/.hivemind/rclone.conf`.

Common lifecycle commands:

| Command | Does |
|---|---|
| `npx -y @browserstack/hivemind up` | Regenerate derived files from conf, then start the stack |
| `npx -y @browserstack/hivemind down` | Stop the stack |
| `npx -y @browserstack/hivemind status` / `logs` | Inspect the running stack |
| `npx -y @browserstack/hivemind doctor` | Check mount / DB / MCP reachability, ACLs, roster, image tag |
| `npx -y @browserstack/hivemind admin provision-user <handle> <email>` | Create + share a member's Drive folder |

Once `up`, Claude Code sees the `hivemind` MCP server at `http://localhost:<mcp_port>/mcp` and can
call `memory_add`, `memory_search`, `memory_list_recent`, and the rest of the tool surface.

---

## Development

This is a monorepo with two independently-tooled packages driven by a root `Makefile`. All targets
self-bootstrap (they create the Python venv and install CLI deps on first use), so a clean checkout
can run them directly.

```sh
make check       # lint + typecheck + unit tests for BOTH packages (green on a clean checkout)
make fmt         # auto-format both packages
make lint        # ruff (app) + eslint/prettier (cli)
make typecheck   # mypy --strict (app) + tsc (cli)
make test        # unit test layer for both packages
make clean       # remove venv / node_modules / caches
```

The `test`/`check` targets run only the **unit layer**. Integration tests that need a real Postgres
are marked `integration` and skip automatically when it is unavailable.

### Python app (`app/`)

- Python, SQLAlchemy 2.0 (async), Pydantic v2, FastMCP + FastAPI, pgvector, sentence-transformers.
- Tooling: `ruff` (format + lint), `mypy --strict`, `pytest` (`asyncio_mode=auto`, `integration` marker).

```sh
cd app
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -m "not integration"          # unit layer (uses the deterministic FakeEmbedder, no downloads)

# Integration tests against a throwaway pgvector instance (test stack, DB only):
docker compose -f docker-compose.test.yml up -d postgres
DATABASE_URL_TEST=postgresql+asyncpg://teamtrail:teamtrail@localhost:5433/hivemind pytest -m integration
```

Two Compose files are maintained: **`docker-compose.test.yml`** (local — pgvector + app, a local
folder as the hive, fake embeddings, no Google Drive) and **`docker-compose.prod.yml`** (production
— the Google Drive FUSE mount + pgvector + app). Run the full local stack with
`docker compose -f docker-compose.test.yml up --build`.

### Node CLI (`cli/`)

- TypeScript (strict), commander, published as `@browserstack/hivemind` (`bin` → `dist/cli.js`).
- Tooling: `eslint` (typescript-eslint), `prettier`, `vitest`, `tsc`.

```sh
cd cli
npm install
npm run typecheck
npm test
npm run build
```

The CLI's file editors (CLAUDE.md block, `.claude.json` / `settings.json` merges, env + Compose
generation, Drive folder-id extraction) are pure functions, so they are unit-tested without touching
your real `~/.claude` or `~/.hivemind`.

---

## Repository layout

```
hivemind/
  app/        Python container app (MCP server + export/import loops)
  cli/        Node/TS host CLI, published as @browserstack/hivemind
  .github/    CI (make check on PRs) + release (npm → GitHub Packages, image → GHCR) on tag
  .claude/    Repo guide + subagents for Claude working in THIS repo
  Makefile    fmt / lint / typecheck / test / check across both packages
  SPEC.md     Implementation spec — the single source of truth
  LICENSE     MIT
```

## Releasing

Pushing a tag (e.g. `v0.1.0`) triggers `.github/workflows/release.yml`, which publishes the CLI to
GitHub Packages and builds + pushes the app image to `ghcr.io/browserstack/hivemind:<tag>`. Pull
requests run `make check`. See the workflow for details.

## License

[MIT](./LICENSE) © 2026 BrowserStack.
