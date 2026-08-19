# Hivemind — Implementation Spec (build contract)

Hivemind is a **serverless, append-only, semantically-searchable shared team memory** exposed
over **MCP**. Every developer runs an identical **local** stack (Docker Compose); nodes sync by
exchanging **immutable NDJSON files** through one shared **Google Drive** folder (`hivemind/`)
mounted with FUSE. Each node holds the whole team's memory in its own local Postgres.

This document is the single source of truth for the implementation, kept in sync with the as-built code. The Mermaid diagrams below show the node architecture, the record lifecycle, and the cross-node sync flow.

---

## 1. Repository layout & ownership

Monorepo root: two deployables plus tooling.

```
hivemind/
  app/                         # Python container app (MCP server + export/import loops)
    hivemind_app/
      __init__.py
      config.py                # Settings (env-driven), Identity, validators
      db.py                    # async engine/sessionmaker, pgvector type registration, Base
      models.py                # SQLAlchemy 2.0 mapped Event (local_events, team_events, ingested_files)
      schemas.py               # Pydantic v2 request/response models (NO identity fields)
      embeddings.py            # Embedder protocol + real (sentence-transformers) + FakeEmbedder
      serialization.py         # NDJSON (de)serialize, atomic write, tolerant parse, folder->author
      repository.py            # async CRUD over Postgres (append-only), hidden-id computation
      service.py               # MemoryService: add/update/delete/get/list/search/history + daily_summary
      loops.py                 # export loop + import loop (filesystem <-> DB)
      mcp_server.py            # FastMCP tools + FastAPI mount + lifespan (schema init, start loops)
      __main__.py              # entrypoint: python -m hivemind_app
    pyproject.toml             # deps + ruff + mypy(strict) + pytest config; sentence-transformers optional [embeddings]
    Dockerfile                 # ARG INSTALL_EMBEDDINGS: 0=torch-free (test), 1=real embedder (prod)
    README.md                  # short: how to run/test the app
  cli/                         # Node/TS host CLI, published as @browserstack/hivemind
    src/
      cli.ts                   # commander entry (bin)
      config.ts                # ~/.hivemind.conf (TOML) load/validate/resolve; folder-id extraction
      paths.ts                 # ~/.hivemind, ~/.claude paths (overridable via env for tests)
      generate.ts              # render ~/.hivemind/{.env, rclone.conf, docker-compose.yml}
      claude.ts                # idempotent editors: CLAUDE.md block, .claude.json, settings.json hook
      guard.ts                 # guard-script installer + probe
      docker.ts                # compose lifecycle helpers
      usage-block.ts           # the CLAUDE.md usage-instructions text
      commands/                # one file per command group
        init.ts config.ts setup.ts lifecycle.ts doctor.ts install.ts admin.ts
    templates/
      docker-compose.yml
      hivemind.conf.example
      rclone.conf.example
    package.json
    tsconfig.json
    eslint.config.js
    .prettierrc.json
    vitest.config.ts
    README.md
  .claude/
    settings.json              # already present (do not modify)
    CLAUDE.md                  # repo guide for Claude working in THIS repo
    agents/                    # helpful subagent definitions (code-reviewer, etc.)
  .github/workflows/release.yml
  docker-compose.test.yml      # LOCAL test stack: pgvector + app, local hive, fake embeddings (no Drive)
  docker-compose.prod.yml      # PROD stack: gdrive FUSE mount + pgvector + app (real embedder)
  Makefile                     # fmt / lint / typecheck / test across both packages
  README.md                    # root: what Hivemind is + quickstart
  LICENSE                      # MIT
  SPEC.md                      # this file
  .gitignore
```

**Ownership (agents write ONLY their subtree — no overlap):**
- `PY_APP`  → everything under `app/` **except** `app/tests/`.
- `NODE_CLI`→ everything under `cli/` **except** `cli/test/`.
- `TOOLING` → root files (`Makefile`, root `README.md`, `LICENSE`) + `.github/**` + `.claude/CLAUDE.md` + `.claude/agents/**`. Do NOT touch `.claude/settings.json`, `app/`, `cli/`.
- `PY_TESTS` → `app/tests/**` only.
- `NODE_TESTS` → `cli/test/**` only.

---

## 2. How it works

Per-node Compose services: **gdrive** (FUSE-mounts `hivemind/` with that developer's OWN Drive
credentials), **postgres** (Postgres 16 + pgvector, LOCAL data volume), **app** (MCP server + two
background loops). Nodes never talk directly; they exchange immutable files via `hivemind/`.

```mermaid
flowchart LR
  agent["Claude / MCP client"]
  subgraph Node["One developer node — Docker Compose"]
    direction TB
    app["app: FastMCP server<br/>+ export & import loops"]
    pg[("postgres + pgvector<br/>local_events · team_events · ingested_files")]
    mount["hive at /mnt/hivemind<br/>(rclone FUSE **or** Google Drive for Desktop)"]
    app <--> pg
    app <--> mount
  end
  agent -->|"MCP HTTP :MCP_PORT"| app
  mount <-->|"sync (owner's Drive credentials)"| drive[("Shared Google Drive<br/>hivemind/&lt;handle&gt;/")]
  drive <-->|"each peer mounts the same hive"| peers["other nodes<br/>(bob, cara, …)"]
```


---

## 3. Trust & identity model

- Drive layout: one folder `hivemind/`, one subfolder per member `hivemind/<handle>/`, each
  **read-write for its owner, read-only for everyone else** (Drive sharing).
- Authorship is derived from **which folder a record sits in**, never from a field in the file.
- Load-bearing condition: each node's gdrive mount authenticates as its OWN developer (per-node
  OAuth), so Drive rejects cross-folder writes. A shared service account voids the guarantee.
- Folder creation is admin-provisioned; the import loop validates every folder against a `roster`
  and ignores unknown folders.

---

## 4. Data model (Postgres DDL — target shape)

Two identical record tables: `local_events` (this node's owner authored — authoritative) and
`team_events` (ingested from others — rebuildable cache), plus `ingested_files` bookkeeping.

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE local_events (
  id           uuid PRIMARY KEY,
  author       text NOT NULL,                 -- always = OWNER_HANDLE
  event_type   text NOT NULL,                 -- enum below
  status       text NOT NULL DEFAULT 'info',  -- open|in_progress|resolved|done|info
  title        text NOT NULL,
  body         text NOT NULL,
  jira_project text NOT NULL,                 -- uppercase, e.g. BETEL
  jira_ticket  text,                          -- PROJECT-123 or NULL
  tags         text[] NOT NULL DEFAULT '{}',
  metadata     jsonb  NOT NULL DEFAULT '{}',
  supersedes   uuid,                          -- set on an update record
  deletes      uuid,                          -- set on a tombstone record
  ts           timestamptz NOT NULL,          -- event time, UTC
  created_at   timestamptz NOT NULL DEFAULT now(),
  embedding    vector(384) NOT NULL,
  exported_at  timestamptz                    -- local_events only; NULL = not yet exported
);
CREATE TABLE team_events (LIKE local_events INCLUDING ALL);

CREATE INDEX ON local_events USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON team_events  USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON local_events (ts);
CREATE INDEX ON team_events  (ts);
CREATE INDEX ON team_events  (author);

CREATE TABLE ingested_files (
  path        text PRIMARY KEY,               -- relative to hivemind, e.g. bob/2026-08-19/....ndjson
  ingested_at timestamptz NOT NULL DEFAULT now()
);
```

Implement the schema via SQLAlchemy models + an idempotent init that runs `CREATE EXTENSION IF NOT
EXISTS vector`, `metadata.create_all`, then `CREATE INDEX IF NOT EXISTS` for the HNSW indexes (raw
SQL). No Alembic. Vector columns are bound and parsed by SQLAlchemy's `pgvector.sqlalchemy.Vector` type; the app does **not** register pgvector's asyncpg codec — it double-encodes with the SQLAlchemy type and fails on a fresh database before the extension exists. `init_schema` creates the extension before `create_all` emits the vector-column DDL.

`event_type` values: `update, decision, blocker, dependency, handoff, research, context, note,
friction, ai_session, tombstone` (tombstone is internal, produced only by delete).
`status` values: `open, in_progress, resolved, done, info`.

---

## 5. File serialization format

- Directory: `hivemind/<handle>/<YYYY-MM-DD>/` (date = `ts` at UTC).
- Immutable batch files named `<epoch_ms>-<uuid4>.ndjson`; never modified/deleted after write.
- Atomic publish: write `<name>.ndjson.tmp`, flush+fsync, `os.rename` to `<name>.ndjson`.
  Importers read only `*.ndjson` (never `*.tmp`).
- NDJSON: one complete JSON object per line; a trailing incomplete line is ignored on read.
- Each record line has exactly these keys:

```json
{
  "id": "uuid", "author": "alice", "event_type": "decision", "status": "info",
  "title": "…", "body": "…", "jira_project": "BETEL", "jira_ticket": "BETEL-42",
  "tags": ["a","b"], "metadata": {}, "supersedes": null, "deletes": null,
  "ts": "2026-08-19T10:00:00Z", "created_at": "2026-08-19T10:00:00Z",
  "embedding": [ /* 384 floats, normalized */ ]
}
```

`embedding` is carried so importers never re-embed. The file's `author` is informational — the
authoritative author is the owning folder handle, which the importer stamps.

---

## 6. Append-only semantics

No row is mutated after insert.
- Create → new record, `supersedes=null`, `deletes=null`.
- Update → new record (new id) with `supersedes=<old_id>`. Allowed only if old record's author ==
  OWNER_HANDLE (own records only).
- Delete → new record, `event_type="tombstone"`, `deletes=<target_id>`, empty title/body,
  zero-vector embedding. Own records only.
- Visible state: `hidden_ids` = {non-null `supersedes`} ∪ {non-null `deletes`} across both tables.
  A record is visible iff its id ∉ hidden_ids and its own event_type != 'tombstone'.
- History for an id = the chain linked by `supersedes`, ordered by `created_at`.

```mermaid
flowchart TB
  A["memory_add → record<br/>supersedes=∅, deletes=∅"] --> V1["visible"]
  V1 -->|"memory_update (own only)"| U["new record<br/>supersedes = old id"]
  U --> V2["new record visible<br/>old id ∈ hidden_ids"]
  V1 -->|"memory_delete (own only)"| T["tombstone record<br/>deletes = target, zero vector"]
  T --> H["target ∈ hidden_ids<br/>tombstone itself excluded"]
  V2 -->|"memory_delete"| T
```


---

## 7. Export & import loops

**Export** (every `EXPORT_INTERVAL_SEC`, default 60):
1. `SELECT * FROM local_events WHERE exported_at IS NULL ORDER BY created_at`. If none, sleep.
2. Group by UTC day of `ts`.
3. For each group: ensure `<hive>/<OWNER_HANDLE>/<day>/`; serialize to a NEW batch file (atomic).
4. `UPDATE local_events SET exported_at = now() WHERE id IN (...)`.

**Import** (every `IMPORT_INTERVAL_SEC`, default 60):
1. List immediate subfolders of `<hive>/`. For each whose name ∈ roster and != OWNER_HANDLE:
   set `author = folder_name`.
2. List `*.ndjson` under it. For each path not in `ingested_files`: read complete lines, set
   `author = folder_name` (ignore file's author), `INSERT ... ON CONFLICT (id) DO NOTHING`
   (embedding from file). After a file is fully processed, record it in `ingested_files`.
3. Never write to others' folders; never import your own folder.

Make the loop bodies callable once (e.g. `export_once()`, `import_once()`) so tests can drive them
deterministically without the sleep scheduler. Inject the hive filesystem root and the clock.

```mermaid
sequenceDiagram
  autonumber
  participant AA as alice · app
  participant AH as alice · hive folder
  participant GD as Google Drive
  participant BH as bob · hive view
  participant BA as bob · app
  AA->>AA: memory_add → insert local_events (author=alice)
  Note over AA,AH: export loop (≤ EXPORT_INTERVAL_SEC)
  AA->>AH: write immutable NDJSON (tmp → atomic rename)
  AH->>GD: sync as alice (write-scoped to alice/)
  GD->>BH: sync alice/ (read-only to bob)
  Note over BH,BA: import loop (≤ IMPORT_INTERVAL_SEC)
  BA->>BH: read new alice/*.ndjson
  BA->>BA: INSERT team_events, author = folder name (alice)
  BA->>BA: memory_search → ranks alice's record beside bob's
```


---

## 8. Embeddings & search

- Model `BAAI/bge-small-en-v1.5`, 384-dim, L2-normalized, cosine (`<=>`). Local, no API/key.
- Embedded text = `title + "\n\n" + body`. Computed at write time; carried in files.
- Define an `Embedder` Protocol with `.dim` and `embed(text) -> list[float]`. Provide
  `SentenceTransformerEmbedder` (lazy-loads the model) and `FakeEmbedder` (deterministic vectors
  from a hash, no downloads) for tests. The real model must NOT load at import time.
- Backend selection (`resolve_embedder`): `EMBEDDING_MODEL=fake` selects `FakeEmbedder` (torch-free tests/CI); any other value uses `SentenceTransformerEmbedder`. `sentence-transformers` is an optional extra (`pip install .[embeddings]`; Dockerfile `ARG INSTALL_EMBEDDINGS`), so the core/test image needs no torch.
- Metadata reads (list_recent): UNION both tables, apply filters, drop hidden_ids/tombstones,
  order by ts DESC, limit.
- Semantic search: embed query; per table `ORDER BY embedding <=> :q LIMIT :pool` with
  `pool = max(top_k, 200)`; union candidates; hydrate; filter; drop hidden_ids/tombstones;
  re-sort by distance; return top_k.

---

## 9. MCP tools (FastMCP, streamable HTTP at MCP_HOST:MCP_PORT/mcp)

All writes are authored as `OWNER_HANDLE` from config — author is NEVER taken from tool input
(reject any attempt to pass author, including inside metadata). Reads span both tables.

- `memory_add(title, body, jira_project, jira_ticket=None, event_type="update", status="info", tags=[], metadata={}, ts=None)`
- `memory_update(id, title, body, jira_project, jira_ticket=None, event_type="update", status="info", tags=[], metadata={})`  (own records only)
- `memory_delete(id)`  (own records only)
- `memory_get(id)`
- `memory_list_recent(limit=50, author=None, event_type=None, status=None, jira_project=None, jira_ticket=None, date_from=None, date_to=None, tags=None)`
- `memory_search(query, top_k=20, <same filters>)`
- `memory_history(id)`
- `memory_daily_summary(day=None, author=None)`
- `me()` -> OWNER_HANDLE ; `author_list()` -> roster

Validation: `jira_project` required, uppercase, regex `^[A-Z][A-Z0-9]+$`; `jira_ticket` if present
matches `^[A-Z][A-Z0-9]+-\d+$`.

The `mcp` SDK is pinned `>=1.2,<2` (the 2.0 release removed `mcp.server.fastmcp`, the FastMCP API this app uses).

---

## 10. Configuration

`~/.hivemind.conf` (TOML) is the human source of truth for the **CLI**. The CLI generates the
container's env from it. Resolution: built-in defaults < `~/.hivemind.conf` < environment overrides.

```toml
# ~/.hivemind.conf
username          = "alice"
mount_backend     = "rclone"                       # "rclone" (default) or "gdrive_desktop"
gdrive_folder_url = "https://drive.google.com/drive/folders/1AbCd...Xyz"  # required for rclone
mcp_port          = 7777
# optional:
# gdrive_desktop_path = "/Users/alice/Library/CloudStorage/GoogleDrive-.../My Drive/hivemind"  # required for gdrive_desktop
# roster              = ["alice","bob","cara"]   # omit -> admin roster.json in the hive
# mount_path          = "/mnt/hivemind"
# export_interval_sec = 60
# import_interval_sec = 60
# image_tag           = "0.1.0"
```

Keys: `username` (required; -> OWNER_HANDLE; handle regex `^[a-z0-9][a-z0-9._-]*$`);
`mount_backend` (`rclone` default | `gdrive_desktop`); `gdrive_folder_url` (**required for
`rclone`**, optional for `gdrive_desktop`; Drive folder id extracted via regex
`/folders/([A-Za-z0-9_-]+)` or `[?&]id=([A-Za-z0-9_-]+)`); `gdrive_desktop_path` (**required for
`gdrive_desktop`**; absolute host path to the `hivemind/` folder inside the Google Drive for
Desktop mount); `mcp_port` (default 7777, 1..65535); `roster` (optional string list); `mount_path`
(rclone FUSE host path, default `/mnt/hivemind`); `export_interval_sec`/`import_interval_sec`
(default 60); `image_tag` (default = CLI package version). Secrets NEVER go in this file (the rclone
Drive OAuth token lives in `~/.hivemind/rclone.conf`; the `gdrive_desktop` backend needs no token).

**App env vars** (generated into `~/.hivemind/.env`): `OWNER_HANDLE`, `ROSTER` (comma-separated),
`HIVEMIND_PATH` (=/mnt/hivemind in-container), `DATABASE_URL`
(`postgresql+asyncpg://teamtrail:teamtrail@postgres:5432/hivemind`), `EMBEDDING_MODEL`
(BAAI/bge-small-en-v1.5), `EMBEDDING_DIM` (384), `EXPORT_INTERVAL_SEC`, `IMPORT_INTERVAL_SEC`,
`MCP_HOST` (0.0.0.0), `MCP_PORT`.

---

## 11. Docker Compose (template shipped by the CLI)

The CLI generates one of two stacks based on `mount_backend`:

- **`rclone`** (default) — three services, described below. Needs a Drive OAuth token.
- **`gdrive_desktop`** — two services (`postgres` + `app`), NO `gdrive`/rclone container and no
  token. Google Drive for Desktop syncs the folder locally; the `app` bind-mounts
  `gdrive_desktop_path` -> `/mnt/hivemind` directly and `depends_on` postgres only.

Three services (rclone backend): `gdrive` (rclone FUSE mount; `cap_add: [SYS_ADMIN]`, `devices: [/dev/fuse]`,
`security_opt: [apparmor:unconfined]`, bind `/mnt/hivemind` with `propagation: rshared`,
healthcheck `test -d /mnt/hivemind/${OWNER_HANDLE}`), `postgres` (`pgvector/pgvector:pg16`, LOCAL
named volume, `pg_isready` healthcheck), `app` (`image: ghcr.io/browserstack/hivemind:<tag>`,
`depends_on` both healthy, env from `.env`, `ports: ["${MCP_PORT}:${MCP_PORT}"]`, bind the mount
`rslave`). Postgres data volume is LOCAL — never on the mount.

---

## 12. CLI commands (host-side)

`init` (scaffold ~/.hivemind.conf), `config [--edit|--print]` (validate/show resolved),
`setup` (read conf, extract folder id, headless `rclone config`, create mount path, generate
`~/.hivemind/{.env,rclone.conf,docker-compose.yml}`, verify ACLs), `up|down|status|logs`
(compose against ~/.hivemind; `up` regenerates derived files from conf first), `doctor` (mount/DB/
MCP reachable + can-write-own / cannot-write-peer + roster ⊇ folders + image tag == CLI version),
`install-usage-instructions` (marker block into ~/.claude/CLAUDE.md, idempotent),
`install-mcp-server` (upsert http entry into ~/.claude.json), `install-enforcement`
(write ~/.hivemind/guard.sh, merge UserPromptSubmit hook pointing at its abs path),
`guard` (HTTP probe of MCP url; exit non-zero if down; honor `HIVEMIND_GUARD_DISABLE=1`),
`admin provision-user <handle> <email>` (Drive API: create folder + share), `install` (one-shot:
setup -> up -> doctor -> the three install-*).

The **guard hook must never call npx** — install a self-contained `~/.hivemind/guard.sh`:
```sh
#!/bin/sh
[ "$HIVEMIND_GUARD_DISABLE" = "1" ] && exit 0
URL="${HIVEMIND_MCP_URL:-http://localhost:7777/mcp}"
curl -s -m 2 -o /dev/null "$URL" && exit 0
echo "Hivemind MCP unreachable — run: npx -y @browserstack/hivemind up (first boot ~60-90s)" >&2
exit 1
```
Hook JSON merged (not overwritten) into ~/.claude/settings.json:
`{"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"<abs>/.hivemind/guard.sh"}]}]}}`.
MCP entry: `{"mcpServers":{"hivemind":{"type":"http","url":"http://localhost:<port>/mcp"}}}`.

The idempotent editors (CLAUDE.md marker block, .claude.json merge, settings.json hook merge,
env generation, folder-id extraction) must be **pure functions** (string/object in -> out) so they
are unit-testable without touching the real home directory.

---

## 13. Distribution

CLI published as private `@browserstack/hivemind` on GitHub Packages
(`publishConfig.registry=https://npm.pkg.github.com`), run via `npx -y @browserstack/hivemind <cmd>`.
`bin` -> `dist/cli.js` (shebang `#!/usr/bin/env node`). App image on GHCR, pinned to CLI version.
Durable state in `~/.hivemind/`. GitHub Actions on tag: build+publish npm (NODE_AUTH_TOKEN =
GITHUB_TOKEN, `permissions: packages: write`) and build+push the app image to GHCR.

---

## 14. Invariants (must hold; tests must assert them)

- Local writes authored as OWNER_HANDLE (config), never client input; imported records take author
  from the read-only folder, roster-validated.
- Append-only files: write-once; updates/deletes are new records.
- Local DB only; only NDJSON files on the mount.
- Eventual consistency (export interval + sync + import interval).
- No secrets in records.

---

## 15. Testing contract (exhaustive)

Both suites must run in CI WITHOUT Docker/Postgres/model downloads for the unit layer; integration
tests that need Postgres are marked and SKIP automatically when it is unavailable.

**Python (`pytest`, `pytest-asyncio`; `app/tests/`):**
- `config`: env/conf resolution, jira regexes, handle regex, defaults, bad values raise.
- `schemas`: author-rejection (author in body/metadata -> error), jira validation, enum coercion.
- `serialization`: NDJSON round-trip; tolerant parse (ignores trailing partial line, blank lines);
  atomic write leaves no `.tmp` and produces valid file; folder->author derivation; day bucketing.
- `append_only`: update sets supersedes; delete emits tombstone; hidden_ids computation; history
  chain ordering; cannot supersede/delete another author's record.
- `embeddings`: FakeEmbedder determinism + dim; embed text builder.
- `service`/`search`: two-table merge, pool over-fetch, hidden filtering, re-rank by distance,
  filter application — drive with a fake repository or fake hits so no DB is needed.
- `loops`: export_once groups by day + writes immutable file + marks exported; import_once is
  idempotent (ON CONFLICT), overwrites author from folder, filters roster, skips own folder,
  tolerates partial files — drive against a tmp hive dir + a fake/real repo.
- `mcp` tools: author always OWNER; author input rejected; filters plumb through.
- INTEGRATION (marked `integration`, skip if no PG): repository CRUD, HNSW search, schema init.
  Provide a `conftest.py` that skips integration when `DATABASE_URL_TEST` is unset or PG down.
  `docker-compose.test.yml` spins up pgvector for local integration runs.
- Target: cover every pure function and every invariant in §14. Use `FakeEmbedder` everywhere in
  unit tests.

**Node (`vitest`; `cli/test/`):**
- `config`: TOML parse, folder-id extraction (folders/ and ?id= forms, reject junk), handle/port
  validation, resolution precedence (defaults < conf < env), missing required -> error.
- `claude` editors: CLAUDE.md marker block insert/replace-in-place/leave-rest; .claude.json
  mcpServers upsert without clobbering siblings; settings.json UserPromptSubmit hook merge is
  idempotent (no duplicate on re-run) and preserves existing hooks.
- `generate`: .env + rclone.conf (root_folder_id) + docker-compose rendering from resolved config.
- `guard`: URL resolution, HIVEMIND_GUARD_DISABLE bypass, guard.sh content.
- Use temp dirs + injected paths; never touch the real ~/.claude or ~/.hivemind.

---

## 16. Tooling

- Python: ruff (format + lint: E,F,I,UP,B,C4,SIM,RUF; line-length 100), mypy strict, pytest with
  `asyncio_mode=auto` and an `integration` marker.
- Node: TypeScript strict, eslint (typescript-eslint), prettier, vitest with coverage.
- Compose helpers auto-detect the launcher: the `docker compose` v2 plugin, falling back to the standalone `docker-compose` binary.
- Root `Makefile` targets: `fmt`, `lint`, `typecheck`, `test`, `check` (all), delegating into both
  packages. `check` must pass on a clean checkout for the unit layer.
- `.claude/CLAUDE.md`: concise repo guide (architecture, invariants from §14, layout, commands).
