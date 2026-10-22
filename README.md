# Hivemind

**Serverless, append-only, semantically-searchable shared team memory — over [MCP](https://modelcontextprotocol.io).**

[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE) ![platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey) ![MCP server](https://img.shields.io/badge/MCP-server-8A2BE2) ![status: alpha](https://img.shields.io/badge/status-alpha-orange)

Hivemind gives an engineering team a single, shared, durable memory of *how work actually
happens* — decisions, blockers, friction, hand-offs, research, and AI-assist notes — that any
teammate's Claude (or other MCP client) can search semantically and append to. There is **no
central server**: every developer runs an identical local stack, and nodes sync by exchanging
**immutable NDJSON files** through one shared **Google Drive** folder. Each node keeps the whole
team's memory in its own local database — embedded **SQLite + sqlite-vec** (zero daemons) or
**Postgres + pgvector**.

**What you get**

- 🧠 Shared memory your agents can `memory_add` / `memory_search` — decisions, blockers, friction, hand-offs, research, AI-assist notes.
- 🚫 **No central server or database** to run or protect — nodes sync immutable files through one Drive folder everyone already has.
- 🔎 **Local semantic search** — on-device embeddings, no API, no key, works offline.
- 🪶 **Two weights:** embedded SQLite+sqlite-vec (no daemon at all) or Postgres+pgvector.
- 🔒 **Unforgeable authorship** — identity comes from the Drive folder, never a field in the file.
- 🧾 **Append-only history** — updates/deletes are new records (supersede / tombstone); nothing is mutated.
- 🔌 **Drops into any MCP client** (Claude Code, …) at `http://localhost:<port>/mcp`.

> **Status:** alpha · macOS (Apple Silicon / Intel) · needs **Nix** *or* **Docker**, plus **Google Drive for Desktop** · the CLI installs from a private GitHub Packages registry (a token with `read:packages`). Details in [Requirements](#requirements).

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

```mermaid
flowchart TB
    subgraph DRIVE["☁️ one shared Google Drive folder — hivemind/"]
        direction LR
        A["alice/&lt;day&gt;/*.ndjson<br/>alice: rw · everyone else: ro"]
        B["bob/&lt;day&gt;/*.ndjson<br/>bob: rw · everyone else: ro"]
    end

    subgraph NODE["💻 each developer's node — identical local stack (Nix process-compose, or Docker)"]
        APP["app<br/>FastMCP server (streamable HTTP /mcp)<br/>+ export loop + import loop"]
        DB[("db — local index<br/>SQLite+sqlite-vec or Postgres+pgvector<br/>LOCAL, never on the mount")]
        APP <--> DB
    end

    CLIENT["Claude Code / any MCP client"]

    APP -- "export loop: write own folder" --> DRIVE
    DRIVE -- "import loop: read all folders" --> APP
    CLIENT -- "MCP over HTTP — localhost:&lt;mcp_port&gt;/mcp" --> APP
```

Two deployables live in this monorepo:

- **`app/`** — the Python container (MCP server + the two background sync loops). Holds the DB
  models, serialization, append-only repository, search service, and the FastMCP tools. Its source
  is **bundled into the CLI package** and run **locally** by `hivemind up` — natively under Nix
  (process-compose / devenv) or as a locally-built Docker image — nothing is pulled from a registry.
- **`cli/`** — the Node/TS host CLI, published as `@crash0v3rrid3/hivemind`. It never contains team
  data; it *provisions the node*: reads `~/.hivemind.conf`, sets up Drive access, generates the stack
  (Nix or Compose) and env, wires Claude (CLAUDE.md block, MCP entry, guard hook), and runs
  `up`/`down`/`status`/`doctor`.

The full data model, file format, loop semantics, MCP tool surface, and invariants are specified in
**[SPEC.md](./SPEC.md)** — the single source of truth for the implementation.

---

## Requirements

- **macOS** (Apple Silicon or Intel) — Hivemind nodes are developer laptops.
- **Nix** with flakes enabled (the default `deploy_backend`), **or** **Docker** (`deploy_backend = "docker"`). If `nix` is unavailable the CLI falls back to Docker automatically.
- **Node.js ≥ 18** to run the CLI via `npx`.
- **GitHub Packages access** — the CLI is published under the `@crash0v3rrid3` scope on GitHub Packages; a GitHub token with `read:packages` is needed to install it.
- **Google Drive for Desktop** syncing a shared `hivemind/` folder: readable by the team, with each member's own subfolder writable (see [SPEC.md](./SPEC.md)).

## Quickstart (via `npx`)

The CLI is published to **GitHub Packages** under the `@crash0v3rrid3` scope. Point npm at that
registry once, then drive everything through `npx`. (Or run `npm i -g @crash0v3rrid3/hivemind` once
and call `hivemind <cmd>` directly — this avoids npx re-resolving the package on every call.)

```sh
# 1. Point the @crash0v3rrid3 scope at GitHub Packages (needs a GitHub token with read:packages),
#    e.g. in ~/.npmrc:
#      @crash0v3rrid3:registry=https://npm.pkg.github.com
#      //npm.pkg.github.com/:_authToken=${GITHUB_TOKEN}

# 2. Scaffold your config (~/.hivemind.conf), then fill in the shared Drive folder + your handle
npx -y @crash0v3rrid3/hivemind init
npx -y @crash0v3rrid3/hivemind config --edit

# 3. One-shot: generate the stack, bring it up, verify, and wire Claude
npx -y @crash0v3rrid3/hivemind install

# First `up` BUILDS the app image locally from bundled source (no registry). The real embedder is
# fastembed/onnxruntime (no torch), so the image is small and the first build is ~1 min; reruns reuse it.
npx -y @crash0v3rrid3/hivemind status
npx -y @crash0v3rrid3/hivemind doctor
```

`~/.hivemind.conf` (TOML) is the only file you edit by hand:

```toml
username            = "alice"
deploy_backend      = "nix"                 # or "docker"; nix requires gdrive_desktop
mount_backend       = "gdrive_desktop"      # or "rclone" (docker backend only)
persistence         = "postgres"            # or "sqlite" (embedded, no daemon)
gdrive_desktop_path = "/Users/alice/Library/CloudStorage/GoogleDrive-alice@corp.com/My Drive/hivemind"
mcp_port            = 7777
```

**Choose a deploy backend** for running the stack:

- `nix` (default, macOS) — runs the app natively via Nix + `process-compose`, no Docker (plus a
  local Postgres when `persistence = "postgres"`).
  Requires `mount_backend = "gdrive_desktop"`. If `nix` isn't installed, the CLI transparently falls
  back to `docker`. The runtime is the bundled app flake (`app/flake.nix`); state lives in
  `~/.hivemind/nix-state`, and the embedding model is fetched once and reused offline. Within
  the nix backend, `process-compose` supervises the stack; if it is unavailable the stack falls
  back to `devenv` (force either with `HIVEMIND_SUPERVISOR=process-compose|devenv`).
- `docker` — runs the stack with Docker Compose. Works with either mount backend.

**Choose a persistence backend** for the local index (shared files are identical either way):

- `postgres` (default) — Postgres 16 + pgvector on a local volume. Heavier, battle-tested.
- `sqlite` — embedded SQLite + sqlite-vec in a single file under the node's state dir. No daemon,
  nothing to run or connect to; ideal for a laptop node.

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
| `npx -y @crash0v3rrid3/hivemind up` | Regenerate derived files from conf, then start the stack |
| `npx -y @crash0v3rrid3/hivemind down` | Stop the stack |
| `npx -y @crash0v3rrid3/hivemind uninstall` | Inverse of `install`: stop + unwire Claude (`--purge` also removes local data; never touches the Drive folder) |
| `npx -y @crash0v3rrid3/hivemind status` / `logs` | Inspect the running stack |
| `npx -y @crash0v3rrid3/hivemind doctor` | Check mount / DB / MCP reachability, ACLs, roster, image tag |
| `npx -y @crash0v3rrid3/hivemind admin provision-user <handle> <email>` | Create + share a member's Drive folder |

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
DATABASE_URL_TEST=postgresql+asyncpg://hivemind:hivemind@localhost:5433/hivemind pytest -m integration
```

Two Compose files are maintained: **`docker-compose.test.yml`** (local — pgvector + app, a local
folder as the hive, fake embeddings, no Google Drive) and **`docker-compose.prod.yml`** (production
— the Google Drive FUSE mount + pgvector + app). Run the full local stack with
`docker compose -f docker-compose.test.yml up --build`.

### Node CLI (`cli/`)

- TypeScript (strict), commander, published as `@crash0v3rrid3/hivemind` (`bin` → `dist/cli.js`).
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
  cli/        Node/TS host CLI, published as @crash0v3rrid3/hivemind
  docs/       Explainer page (served via GitHub Pages)
  examples/   End-to-end walkthrough (two-person hive)
  .github/    CI + release workflows, issue/PR templates
  Makefile    fmt / lint / typecheck / test / check across both packages
  SPEC.md     Implementation spec — the single source of truth
  LICENSE     MIT
```

## Project

- **[Explainer](https://crash0v3rrid3.github.io/hivemind/)** — the one-page "why & how" (also in [`docs/`](./docs/index.html)).
- **[Example walkthrough](./examples/README.md)** — a two-person hive in five minutes.
- **[SPEC.md](./SPEC.md)** — the single source of truth for behavior and invariants.
- **[Contributing](./CONTRIBUTING.md)** · **[Security](./SECURITY.md)** · **[Changelog](./CHANGELOG.md)**

## Releasing

Pushing a tag (e.g. `v0.1.0`) triggers `.github/workflows/release.yml`, which publishes the CLI to
GitHub Packages. The package is **self-sufficient** — it bundles the app source and `hivemind up` builds the
image locally — so nothing is pulled from a registry at runtime. A stack change ships as a new
package version, which rebuilds the local image
on upgrade. Pull requests run `make check`. See the workflow for details.

## License

[MIT](./LICENSE) © 2026 Rishab Jain.
