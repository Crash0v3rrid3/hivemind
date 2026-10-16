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
  time with a local **fastembed/onnxruntime** model (no torch, no API, no key) and carried in the
  files so importers never re-embed. The default `embeddings=true` bakes the small ONNX model into
  the image at build; set `embeddings=false` for an instant, model-free fake-embedder image for
  quick trials.

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
  models, serialization, append-only repository, search service, and the FastMCP tools. Its source
  is **bundled into the CLI package** and the image is **built locally** by `hivemind up` (tagged
  `hivemind:<version>`) — nothing is pulled from a registry.
- **`cli/`** — the Node/TS host CLI, published as `@browserstack/hivemind`. It never contains team
  data; it *provisions the node*: reads `~/.hivemind.conf`, sets up the rclone mount, generates the
  Compose stack and env, wires Claude (CLAUDE.md block, MCP entry, guard hook), and runs
  `up`/`down`/`doctor`.

The full data model, file format, loop semantics, MCP tool surface, and invariants are specified in
**[SPEC.md](./SPEC.md)** — the single source of truth for the implementation.

---

## Requirements

- **macOS** (Apple Silicon or Intel) — Hivemind nodes are developer laptops.
- **Nix** with flakes enabled (the default `deploy_backend`), **or** **Docker** (`deploy_backend = "docker"`). If `nix` is unavailable the CLI falls back to Docker automatically.
- **Node.js ≥ 18** to run the CLI via `npx`.
- **Google Drive for Desktop** syncing a shared `hivemind/` folder: readable by the team, with each member's own subfolder writable (see [SPEC.md](./SPEC.md)).
- Access to the private `@browserstack` GitHub Packages registry (a GitHub token with `read:packages`).

## Quickstart (via `npx`)

The CLI is distributed on the private `@browserstack` GitHub Packages registry, so configure npm for
the `@browserstack` scope once, then drive everything through `npx`. (Or run
`npm i -g @browserstack/hivemind` once and call `hivemind <cmd>` directly — this avoids npx
re-resolving the package on every call.)

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

# First `up` BUILDS the app image locally from bundled source (no registry). The real embedder is
# fastembed/onnxruntime (no torch), so the image is small and the first build is ~1 min; reruns reuse it.
npx -y @browserstack/hivemind status
npx -y @browserstack/hivemind doctor
```

`~/.hivemind.conf` (TOML) is the only file you edit by hand:

```toml
username            = "alice"
deploy_backend      = "nix"                 # or "docker"; nix requires gdrive_desktop
mount_backend       = "gdrive_desktop"      # or "rclone" (docker backend only)
gdrive_desktop_path = "/Users/alice/Library/CloudStorage/GoogleDrive-alice@corp.com/My Drive/hivemind"
mcp_port            = 7777
```

**Choose a deploy backend** for running the stack:

- `nix` (default, macOS) — runs Postgres + the app natively via Nix + `process-compose`, no Docker.
  Requires `mount_backend = "gdrive_desktop"`. If `nix` isn't installed, the CLI transparently falls
  back to `docker`. The runtime is the bundled app flake (`app/flake.nix`); state lives in
  `~/.hivemind/nix-state`, and the embedding model is fetched once and reused offline. Within
  the nix backend, `process-compose` supervises the stack; if it is unavailable the stack falls
  back to `devenv` (force either with `HIVEMIND_SUPERVISOR=process-compose|devenv`).
- `docker` — runs the stack with Docker Compose. Works with either mount backend.

**Choose a mount backend** for getting the shared Drive folder onto the node:

- `rclone` (default) — mounts Drive via the rclone FUSE container. Needs a Drive OAuth token
  (created by `setup`, stored in `~/.hivemind/rclone.conf`). Requires `gdrive_folder_url`.
- `gdrive_desktop` — no rclone, no OAuth `client_id`/`secret`, no token on disk. You run
  [Google Drive for Desktop](https://support.google.com/a/users/answer/13022292); it syncs the
  folder locally and Compose bind-mounts it. Set `gdrive_desktop_path` to the `hivemind/` folder
  inside that mount (e.g. `/Users/alice/Library/CloudStorage/GoogleDrive-alice@corp.com/My Drive/hivemind`).

Everything else — the container `.env`, (for rclone) `rclone.conf`, and `docker-compose.yml` — is
generated into `~/.hivemind/` from that config. **Secrets never go in `~/.hivemind.conf`.**

Common lifecycle commands:

| Command | Does |
|---|---|
| `npx -y @browserstack/hivemind up` | Regenerate derived files from conf, then start the stack |
| `npx -y @browserstack/hivemind down` | Stop the stack |
| `npx -y @browserstack/hivemind uninstall` | Inverse of `install`: stop + unwire Claude (`--purge` also removes local data; never touches the Drive folder) |
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

- Python, SQLAlchemy 2.0 (async), Pydantic v2, FastMCP + FastAPI, pgvector, fastembed (onnxruntime, no torch).
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
GitHub Packages. The package is **self-sufficient** — it bundles the app source and `hivemind up`
builds the image locally — so publishing the app image to GHCR is an **optional** convenience, not
required at runtime. A stack change ships as a new package version, which rebuilds the local image
on upgrade. Pull requests run `make check`. See the workflow for details.

## License

[MIT](./LICENSE) © 2026 BrowserStack.
