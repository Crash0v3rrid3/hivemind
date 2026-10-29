# Hivemind — Implementation Spec (build contract)

Hivemind is a **serverless, append-only, semantically-searchable shared team memory** exposed
over **MCP**. Every developer runs a **local** stack (Docker Compose or native Nix); nodes sync by
exchanging **immutable NDJSON files** through one shared **Google Drive** folder (`hivemind/`)
mounted with FUSE. Each node holds the whole team's memory in its own local SQLite + sqlite-vec or Postgres + pgvector database.

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
      embeddings.py            # Embedder protocol + real (fastembed / onnxruntime, no torch) + FakeEmbedder
      serialization.py         # NDJSON (de)serialize, atomic write, tolerant parse, folder->author
      repository.py            # async CRUD over SQLite/Postgres (append-only), hidden-id computation
      service.py               # MemoryService: add/update/delete/get/list/search/history + daily_summary
      loops.py                 # export loop + import loop (filesystem <-> DB)
      mcp_server.py            # FastMCP tools + FastAPI mount + lifespan (schema init, start loops)
      __main__.py              # entrypoint: python -m hivemind_app
    pyproject.toml             # deps + ruff + mypy(strict) + pytest config; fastembed optional [embeddings] (onnxruntime, no torch)
    Dockerfile                 # ARG INSTALL_EMBEDDINGS: 0=fake embedder (test), 1=real fastembed/onnx (prod)
    README.md                  # short: how to run/test the app
  cli/                         # Node/TS host CLI, published as @crash0v3rrid3/hivemind
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
  subgraph Node["One developer node — Docker Compose or Nix"]
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
  "schema_version": 2,
  "id": "uuid", "author": "alice", "event_type": "decision", "status": "info",
  "title": "…", "body": "…", "jira_project": "BETEL", "jira_ticket": "BETEL-42",
  "tags": ["a","b"], "metadata": {}, "supersedes": null, "deletes": null,
  "ts": "2026-08-19T10:00:00Z", "created_at": "2026-08-19T10:00:00Z",
  "embedding": [ /* 384 floats, normalized */ ],
  "embedding_model": "BAAI/bge-small-en-v1.5"
}
```

`embedding` is carried so importers never re-embed. The file's `author` is informational — the
authoritative author is the owning folder handle, which the importer stamps.

**Model provenance.** New records carry `embedding_model`. Bound databases reject
missing or mismatched provenance; see [upgrade instructions](docs/review-remediation.md).
The database is also bound to owner and hive identity, and cannot be silently reused.

**Format version.** `schema_version` is the wire-format version, independent of the
application's alpha package version. Current writers emit **version 2**, which requires a
nonempty `embedding_model`. Readers accept integer versions `1` and `2` only; unsupported
versions, noninteger versions and v2 records missing valid provenance are rejected. Unknown
optional keys within a supported version are ignored. Further incompatible wire changes must
increment this schema version and document the upgrade procedure.

A missing version means legacy v1. V1 records decode with **unknown model provenance**, even
if an optional extension key claims a model. Parsing v1 exists for offline inspection and
explicit migration, not automatic production import: bound repositories reject these records.
The low-level serializer can emit v1 when handed a record with unknown provenance, for legacy
migration tooling; production service writes and bound-repository exports carry a model and
therefore emit v2. This is a deliberate compatibility break, not an additive v1 field.

Upgrade all hive members together after backing up their databases. Old readers may ignore
schema versions and cannot safely enforce v2 provenance; mixed old/new nodes are unsupported.
Follow the [migration and republishing procedure](docs/review-remediation.md) before resuming
shared writes. No existing archive is silently relabeled with a guessed model.

---

## 6. Append-only semantics

No row is mutated after insert.
- Create → new record, `supersedes=null`, `deletes=null`.
- Update → new record (new id) with `supersedes=<old_id>`. Allowed only if old record's author ==
  OWNER_HANDLE (own records only).
- Delete → new record, `event_type="tombstone"`, `deletes=<target_id>`, empty title/body,
  zero-vector embedding. Own records only.
- Visible state: `hidden_ids` contains authorized same-author `supersedes` and `deletes`
  targets across both tables. Conflicting UUIDs are quarantined and excluded.
  A record is visible iff its id ∉ hidden_ids and its own event_type != 'tombstone'.
- History for an id = the connected component of authorized `supersedes` edges,
  including every branch and the requested anchor, ordered by `created_at` and id.

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
1. Read a bounded batch of pending local records, including original records whose IDs are
   quarantined. Exporting this original evidence lets peers detect the same conflicts;
   quarantine still excludes conflicting IDs from visible reads. If none, sleep.
2. Group by UTC day of `ts`.
3. Require the existing hive and provisioned owner folder; create only its day directory
   and serialize to a NEW batch file (atomic).
4. `UPDATE local_events SET exported_at = now() WHERE id IN (...)`.

**Import** (every `IMPORT_INTERVAL_SEC`, default 60):
1. List immediate subfolders of `<hive>/`. Include the owner folder and peers in the
   configured roster (or validated admin `roster.json` when present). For each:
   set `author = folder_name`.
2. List `*.ndjson` under it. Defer incomplete files; validate complete records and vectors,
   stamp `author = folder_name`, and import in a transaction with its ingested marker.
   Equal UUIDs with different payloads are quarantined. A bad file does not stop later files.
3. Owner files restore local rows marked already exported; peer files populate team rows.
   Never write to other members' folders.

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
- Define an `Embedder` Protocol with `.dim` and `embed(text) -> list[float]`. Provide the real
  embedder backed by **fastembed** (onnxruntime, no torch) that lazy-loads the ONNX model, and
  `FakeEmbedder` (deterministic vectors from a hash, no downloads) for tests. The real model must
  NOT load at import time.
- Backend selection (`resolve_embedder`): `EMBEDDING_MODEL=fake` selects `FakeEmbedder` (dependency-free tests/CI); any other value uses the fastembed embedder. `fastembed`/`onnxruntime` are an optional extra (`pip install .[embeddings]`; Dockerfile `ARG INSTALL_EMBEDDINGS`), so the core/test image needs neither. With `INSTALL_EMBEDDINGS=1` the small ONNX model is baked into the image at build time, so the first query is instant and needs no network; because there is no torch, the image is much smaller (~300-400 MB vs ~2 GB) and builds in ~1 min.
- Metadata reads (list_recent): UNION both tables, apply filters, drop hidden_ids/tombstones,
  order by ts DESC, limit.
- Semantic search: embed off the event loop; filter visible, non-conflicting records
  in SQL before exact distance ranking and limits; merge and return top_k. Result
  limits are strict integers from 1 to 1000. Daily summaries date-filter before aggregation.

- `memory_search` defaults to semantic; explicit `keyword` and `hybrid` modes preserve
  the same list-of-public-records response. Invalid modes fail through MCP validation.
- Keyword search is lexical substring term matching, **not BM25**: queries are ASCII-lower
  normalized, deduplicated and bounded to 32 terms, retaining code identifiers,
  paths and tickets. SQL lowercases both stored fields and patterns; Unicode case behavior is backend-dependent
  (SQLite lower is ASCII-only). Non-ASCII identifiers are preserved literally. Per-term title/body/ticket/tag matches
  weigh 4/1/8/3; repeated occurrences do not increase the score. Literal `%`, `_` and
  backslashes are escaped parameters, never query syntax. Keyword mode never embeds.
- Hybrid fetches `max(200, top_k)` eligible candidates per channel and applies equal-weight
  reciprocal rank fusion with constant 60 and one contribution per ID per channel.
  Lexical-only hits survive fusion. All rankings break ties by record ID ascending;
  visibility and requested filters apply before each channel's ranking and limit.
  Internal `ranked_search` returns record, fused score and matched channels for context
  consumers; embeddings and arbitrary metadata are never added to public search output.

---

## 9. MCP tools (FastMCP, streamable HTTP at MCP_HOST:MCP_PORT/mcp)

All writes are authored as `OWNER_HANDLE` from config — author is NEVER taken from tool input
(reject any attempt to pass author, including inside metadata). Reads span both tables.

- `memory_add(title, body, jira_project, jira_ticket=None, event_type="update", status="info", tags=[], metadata={}, ts=None)`
- `memory_update(id, title, body, jira_project, jira_ticket=None, event_type="update", status="info", tags=[], metadata={})`  (own records only)
- `memory_delete(id)`  (own records only)
- `memory_get(id)`
- `memory_list_recent(limit=50, author=None, event_type=None, status=None, jira_project=None, jira_ticket=None, date_from=None, date_to=None, tags=None)`
- `memory_search(query, top_k=20, <same filters>, mode="semantic")`
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
# embeddings          = true   # true = real BAAI embedder (fastembed/onnx); false = fake (instant)
# image_tag           = "0.3.0"
```

Keys: `username` (required; -> OWNER_HANDLE; handle regex `^[a-z0-9][a-z0-9._-]*$`);
`mount_backend` (`rclone` default | `gdrive_desktop`); `gdrive_folder_url` (**required for
`rclone`**, optional for `gdrive_desktop`; Drive folder id extracted via regex
`/folders/([A-Za-z0-9_-]+)` or `[?&]id=([A-Za-z0-9_-]+)`); `gdrive_desktop_path` (**required for
`gdrive_desktop`**; absolute host path to the `hivemind/` folder inside the Google Drive for
Desktop mount); `mcp_port` (default 7777, 1..65535); `roster` (optional string list); `mount_path`
(rclone FUSE host path, default `/mnt/hivemind`); `export_interval_sec`/`import_interval_sec`
(default 60); `embeddings` (bool, default true; true = bake the real fastembed/onnx embedder plus its
ONNX model into the image, false = a lean fake-embedder image + `EMBEDDING_MODEL=fake`; env
`HIVEMIND_EMBEDDINGS`); `image_tag` (default =
CLI package version — the locally-built image tag). Secrets NEVER go in this file (the rclone
Drive OAuth token lives in `~/.hivemind/rclone.conf`; the `gdrive_desktop` backend needs no token).

**App env vars** (generated into `~/.hivemind/.env`): `OWNER_HANDLE`, `ROSTER` (comma-separated),
`HIVEMIND_PATH` (=/mnt/hivemind in-container), `DATABASE_URL`
(`postgresql+asyncpg://hivemind:hivemind@postgres:5432/hivemind`), `EMBEDDING_MODEL`
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
named volume, `pg_isready` healthcheck), `app` (built LOCALLY — see below —
`depends_on` both healthy, env from `.env`, `ports: ["${MCP_PORT}:${MCP_PORT}"]`, bind the mount
`rslave`). Postgres data volume is LOCAL — never on the mount.

**The app image is built locally, never pulled (self-sufficient).** The `app` service uses
`build: { context: ./app, args: { INSTALL_EMBEDDINGS: "0|1" } }` + `image: hivemind:<version>`.
`setup`/`up` stage the app source (bundled in the CLI package) into `~/.hivemind/app`, so
`docker compose up` builds `hivemind:<version>` on first run, reuses it afterwards, and rebuilds
when a package upgrade bumps the version. No registry is required at runtime. The
`embeddings` config toggle sets `INSTALL_EMBEDDINGS` (1 = real fastembed/onnxruntime embedder with
the ONNX model baked in — no torch, so the image is small and builds in ~1 min; 0 = lean
fake-embedder image + `EMBEDDING_MODEL=fake`).

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

Inverses: `uninstall-usage-instructions`, `uninstall-mcp-server`, `uninstall-enforcement`
(pure, idempotent removals that preserve siblings/other hooks + delete `guard.sh`), and
`uninstall` (one-shot: `down` + the three above; `--purge` also drops the Postgres volume,
`~/.hivemind`, and the local `hivemind:<version>` image). Uninstall NEVER touches the shared
Drive folder — that is the team's memory, not local node state.

The **guard hook must never call npx**. The self-contained guard checks `/healthz`
for HTTP 200 plus `service: hivemind` and `status: ok`, then exits 0. Failures exit 2
to block `UserPromptSubmit`; `HIVEMIND_GUARD_DISABLE=1` explicitly bypasses it.
The hook command shell-quotes the absolute script path.
Hook JSON merged (not overwritten) into ~/.claude/settings.json:
`{"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"<abs>/.hivemind/guard.sh"}]}]}}`.
MCP entry: `{"mcpServers":{"hivemind":{"type":"http","url":"http://localhost:<port>/mcp"}}}`.

The idempotent editors (CLAUDE.md marker block, .claude.json merge, settings.json hook merge,
env generation, folder-id extraction) must be **pure functions** (string/object in -> out) so they
are unit-testable without touching the real home directory.

---

## 13. Distribution

CLI published as `@crash0v3rrid3/hivemind` on GitHub Packages (`publishConfig.registry=https://npm.pkg.github.com`), run via `npx -y @crash0v3rrid3/hivemind <cmd>`.
`bin` -> `dist/cli.js` (shebang `#!/usr/bin/env node`). Durable state in `~/.hivemind/`.
Tip: `npm i -g @crash0v3rrid3/hivemind` installs the `hivemind` binary once and lets you call
`hivemind <cmd>` directly, avoiding npx re-resolving the package on every invocation.

**Self-sufficient — the package ships everything to run.** The build bundles the Python app source
(`app/` → Dockerfile, pyproject, `hivemind_app/**`) into the package (`package.json` `files`
includes `app/`), and `hivemind up` builds the image locally from it. The **image/stack is coupled
to the CLI package version**: any stack change ships as a new package version, which changes the
`hivemind:<version>` tag and triggers a rebuild on upgrade. No registry pull is needed at runtime.

GitHub Actions on tag: build + publish the CLI to GitHub Packages (NODE_AUTH_TOKEN = GITHUB_TOKEN). The app image
is always built locally by `up`, so no image registry is involved.

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
  idempotent (ON CONFLICT), overwrites author from folder, filters roster, restores own folder,
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

### Task context packs

Before work, call `memory_context(task, jira_project=None, jira_ticket=None,
 token_budget=2000, max_items=12)` to read relevant sources. It performs hybrid
retrieval over up to 200 ranked candidates using existing visibility and project/
ticket filters; there is no repository filter. It never writes automatically.
Task must be nonblank and at most 10,000 characters. Budget and item limits are
strict integers (256–16,000 and 1–50). A task that cannot fit the response envelope
raises a validation error.

The pack includes task, source items, estimated_tokens, token_budget, truncated,
and budget_method. Each item attributes source_id, author, event_type, status,
title, timestamp, retrieval channels and reason; excerpts contain only original
body prefixes. No embeddings or arbitrary metadata are exposed. Identical titles
and bodies (NFKC, casefold, collapsed whitespace) are deduplicated, keeping the
first in deterministic relevance order, with ID ties inherited from retrieval.
Categories explain relevance without overriding ranking. Oversized items may be
omitted; excerpts are shortened at Unicode character boundaries. `truncated`
indicates budget or max_items omissions within the candidate window.

The estimator is ceil(UTF-8 bytes / 4) over the **entire pack JSON**, including
all envelope fields and the estimate itself, with two-space indentation and
unescaped Unicode, matching MCP text serialization. It is approximate, **not a
tokenizer guarantee**; clients that reserialize should use the documented format
when comparing the estimate. Very long titles may leave no room for an item.

### Local sync status

`GET /sync/status` and the zero-argument MCP tool `memory_sync_status()` return
identical local observations. Status queries perform database aggregate counts,
never filesystem scans. This per-process telemetry resets on restart.

| Field | Type / meaning |
| --- | --- |
| `service` | string, always `hivemind` |
| `health` | `unknown` before both directions have completed clean cycles, `ok` after clean cycles, `degraded` while a cycle has problems, its failed outcome awaits a clean retry, or quarantine is nonempty |
| `telemetry_started_at` | UTC ISO 8601 string |
| `loops_enabled` | boolean, whether this process starts background loops |
| `remote_delivery` | string, always `unknown`; no peer acknowledgement is inferred |
| `pending_local_exports` | nonnegative integer; exact unpublished local row count, including conflict evidence, independent of export batch limit |
| `quarantined_ids` | nonnegative integer; exact quarantined UUID count |
| `import`, `export` | cycle objects described below |
| `recovery_guidance` | bounded list of fixed recovery strings for current problem categories and conflicts |

Each cycle object contains `last_attempt_at` and `last_success_at` (UTC ISO 8601
strings, null before an attempt/success), `last_failure_category` (null or a
stable category string), `problem_count` (total failed/deferred files or cycle
problems in the latest attempt), and `problems` (at most 20 objects with `path`
relative to the hive root, or null for cycle failures, and `reason`). Paths use
UTF-8 percent encoding (POSIX undecodable filename bytes are preserved), with
`/` separators and unreserved ASCII characters left literal. Encoded paths are
at most 512 ASCII bytes; longer path samples are omitted, while total problem
counts, failure categories, and recovery guidance are retained. Thus the maximum
40 samples across both cycles fit within the CLI's 64 KiB response cap. Consumers
must not decode paths for terminal output without escaping control characters. Categories
are `missing_mount`, `roster`, `read`, `parse`, `incomplete`, `import`, `export`.
Starting a retry clears current problem samples but retains its previous failure category
until clean completion, so an in-progress retry cannot falsely report recovery.
A clean completed attempt clears the failure category; partial imports never
advance `last_success_at`. Missing roots and invalid/unreadable rosters never
count as successful cycles. An absent optional roster retains folder discovery
semantics. Publication success means writing local immutable files and marking
local rows exported; it says nothing about delivery to another device.
No exceptions, credentials, record content, or absolute hive paths are included.
