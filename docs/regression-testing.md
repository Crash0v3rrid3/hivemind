# Released-feature regression testing

`SPEC.md` defines the released 0.3 behavior. This matrix names reproducible scenarios,
not all possible inputs or interleavings. Tests use temporary state, fake embeddings,
local HTTP servers and controlled external boundaries; no Drive credentials or model
downloads are required. Never run these commands against production databases.

From the repository root:

```sh
make regression
# Equivalent layers:
make check
make eval-retrieval
npm --prefix cli run smoke:package
# Separate real PostgreSQL layer (disposable PostgreSQL 16 + pgvector database):
DATABASE_URL_TEST=postgresql+asyncpg://hivemind:hivemind@localhost:5432/hivemind \
  app/.venv/bin/python -m pytest app -m integration -v
# Inspect the new cross-member PostgreSQL selection without connecting:
app/.venv/bin/python -m pytest app/tests/test_member_regression.py -m integration --collect-only -q
```

`make check` bootstraps dependencies, runs Ruff formatting/lint, strict mypy,
ESLint, TypeScript and Python/CLI unit tests. It excludes the integration marker.
`make regression` adds the offline SQLite retrieval evaluator and builds/packs the
CLI before testing the extracted artifact. PostgreSQL is deliberately separate:
`DATABASE_URL_TEST` must name a disposable database with extension/table and
CREATE/DROP SCHEMA privileges. Existing integration fixtures recreate their tables;
new member fixtures own randomly named schemas, assert table namespace/OID isolation,
and drop only those schemas. Unset database configuration skips integration cases;
configured connection/schema failures fail the new fixture loudly.

## Backend feature and invariant matrix

Paths below are relative to `app/tests/`. “Members” means
`test_member_regression.py`: 15 SQLite instances and the same 15 PostgreSQL instances
selected by `-m integration`, with real repositories/services/files. Other PostgreSQL
backend checks live in `test_integration.py`; `test_integration_fixture.py` tests fixture
failure behavior. Unit rows default to SQLite/fakes unless stated otherwise.

| Public feature / invariant | Exact test modules / layer | Protected scenarios and failure cases |
| --- | --- | --- |
| Configuration, owner, roster and metadata validation | `test_config.py`, `test_schemas.py`; unit | Defaults/env, handles, projects/tickets, enums, payload limits, invalid identities and author metadata rejection. |
| `memory_add`, `memory_update`, `memory_delete` | `test_append_only.py`, `test_service_regressions.py`, Members; unit + PG | Owner-derived author, immutable originals/replacements/tombstones, forbidden foreign mutation, exact survivors, missing IDs and malformed arguments. |
| `memory_get`, `memory_list_recent`, `memory_history` | `test_append_only.py`, `test_service_search.py`, `test_storage_regressions.py`, Members; unit + PG | Both tables, visibility, author/metadata/date filters, stable ordering, branch history, cross-author edges, hidden rows do not consume limits. |
| `memory_search` semantic / keyword / hybrid | `test_service_search.py`, `test_service_regressions.py`, `test_retrieval.py`, `test_integration.py`, Members; unit + PG | Filter-before-limit, literal wildcard/path/ticket matching, lexical score/RRF ties, keyword avoids embedding, lexical-only hits, exact visible ID sets and two-table candidate pools. |
| `memory_daily_summary` | `test_service_search.py`, `test_service_regressions.py`, Members; unit + PG | Exact day/author counts, hidden rows excluded, historical day unaffected by global cutoffs. |
| `memory_context` | `test_context.py`, Members; unit + PG | Source IDs, filters, deduplication, relevance ordering, Unicode, whole-envelope budget, oversized title/task, invalid limits and booleans. |
| `me`, `author_list`, registered MCP tool schemas | `test_mcp.py`, `test_context.py`, `test_service_regressions.py`, Members; unit + PG | Actual dispatcher calls all 12 tools; unknown arguments rejected for every tool; owner/roster identity and HTTP author rejection. |
| HTTP `/healthz`, `/sync/status`, MCP transport | `test_mcp.py`, `test_sync_status.py`, `test_service_regressions.py`; unit/local HTTP | Identity/service health responses, exact telemetry/tool snapshots, strict input and oversize errors. This is local transport, not a deployed proxy test. |
| Wire schema v2, immutable NDJSON publication, model provenance | `test_serialization.py`, `test_storage_regressions.py`, `test_loops.py`, Members; unit + PG | Round trips, atomic files/day groups, unknown schema/model or invalid vector, owner attribution overrides forged payload author, partial/truncated/malformed files atomic and retryable, `.tmp` ignored. |
| Import/export, roster discovery and owner restore | `test_loops.py`, `test_convergence.py`, `test_storage_regressions.py`, Members; unit + PG | Repeat delivery idempotency, admin roster errors/exclusion, UTF-8 failure isolation, large batches, missing mount/wrong owner, recovery of archived own records without republishing. |
| UUID conflict quarantine and pending evidence | `test_storage_regressions.py`, `test_append_only.py`, Members; unit + PG | Different observation orders hide conflicts; conflicting pending owner evidence still exports and quarantines peers; exact pending/conflict counts and no resurrection. |
| Offline multi-member convergence and durable restart | Members; unit + PG | Three independent DBs and hive views; offline/reordered/repeated replacement and tombstone batches; restart reconstructs repositories/services over persistent state; seeds 0, 19, 307 assert independently authored six-survivor oracle. |
| Sync failure, retry and background export lifecycle | `test_sync_status.py`, `test_loops.py`, Members; unit + PG | Failure before publication, after publication at bookkeeping, and within import transaction; no lost pending rows or partial commit/marker; retry health, retained success times, bounded stop/retry schedule. |
| Sync privacy and bounded telemetry | `test_sync_status.py`, `test_storage_regressions.py`; unit | Counts beyond batch size, unknown/ok/degraded cycles, traversal errors, bounded samples/path encoding, redaction and transport response cap. |
| SQLite/PostgreSQL persistence and indexes | `test_sqlite_backend.py`, `test_integration.py`, Members; unit + PG | Schema idempotence, timestamps, nearest/recent/history across tables, durable markers, HNSW existence, large/filtered imports; member schema/OID and pre-delivery row independence. |
| Embedding adapters and retrieval evaluator | `test_embeddings.py`, `test_evaluate_retrieval.py`; unit; `make eval-retrieval` | Deterministic vectors, embedded text and optional adapter boundaries; curated synthetic ranking, exact identifiers, context budgets and zero visibility leaks. Optional real-model smoke may skip. |
| Legacy state migration | `test_migrate_state.py`; unit | Owner/model validation and idempotent migration; this is not comprehensive migration CLI/error coverage. |

## CLI command and package matrix

Paths below are relative to `cli/`. Unit tests run via `npm --prefix cli test`;
`smoke` means `scripts/smoke-package.mjs` through `npm --prefix cli run smoke:package`.
Smoke executes the extracted CLI in child processes with temporary HOME/cwd, installed
production dependencies staged offline, and recording/failing service-tool stubs.

| Public command family / surface | Exact test paths / layer | Protected scenarios and failure cases |
| --- | --- | --- |
| `init`, `config` | `test/config.test.ts`, `test/released-adversarial.test.ts`; unit + smoke | Template fidelity, preserve existing config, defaults/env precedence, duplicate TOML keys, hostile handles, bad ports/roster, missing/invalid config, input immutability. |
| `setup` | `test/commands.test.ts`, `test/generate.test.ts`, `test/utilities-hardening.test.ts`; unit + smoke help | Folder ID/env/compose generation, OAuth failure preserves config, shell quoting and mount validation; external tools mocked. Smoke covers CLI registration/help, not successful OAuth. |
| `up`, `down`, `status`, `logs` | `test/commands.test.ts`, `test/docker.test.ts`, `test/nix.test.ts`, `test/released-adversarial.test.ts`; unit + smoke | Backend selection/records, generated runtime inputs, corrupt backend record rejection, record-only cleanup, invalid-config up fails before any service invocation. Actual service boot is separate. |
| `doctor` | `test/commands.test.ts`, `test/docker.test.ts`, `test/nix.test.ts`; unit + smoke help | Own/peer ACL checks, effective roster, SQLite container probe, backend command boundaries and version checks; mocked external DB/mount/service operations. |
| `admin provision-user` | `test/commands.test.ts`, `test/utilities-hardening.test.ts`; unit + smoke help | Mocked Drive API retry, ambiguous folder and permission-conflict handling; no live Drive creation, sharing or ACL propagation. |
| `install-usage-instructions`, `uninstall-usage-instructions` | `test/claude.test.ts`; unit + smoke help | Marker edits idempotent, preserve unrelated text. |
| `install-mcp-server`, `uninstall-mcp-server` | `test/claude.test.ts`, `test/released-adversarial.test.ts`; unit + smoke help | Preserve sibling MCP entries; malformed JSON never overwritten. |
| `install-enforcement`, `uninstall-enforcement` | `test/claude.test.ts`, `test/released-adversarial.test.ts`, `test/guard.test.ts`; unit + smoke help | Hook migration/merge/removal preserves metadata, sibling events and input objects; quoted paths and self-contained guard. |
| `guard` | `test/guard.test.ts`; unit + smoke subprocess | Healthy/unhealthy HTTP identity, bypass and failure exit; generated shell guard tested locally, no npx dependency. |
| `install`, `uninstall` / `--purge` | `test/commands.test.ts`; unit + smoke help | Failed doctor prevents editor activation, local uninstall/purge command safety and shared-folder preservation at mocked service boundaries. |
| `sync status` / `--json` | `test/sync.test.ts`, `test/fixtures/sync-path-status.json`; unit + smoke | Success/degraded/unknown text and JSON, unusual path fixture fidelity, malformed JSON/schema, truncated/oversize body, redirect, 503, closed port; GET-only endpoint, payload redaction and read-only config/home. |
| Paths, generation, Docker/Nix adapters | `test/paths.test.ts`, `test/generate.test.ts`, `test/docker.test.ts`, `test/nix.test.ts`, `test/utilities-hardening.test.ts`; unit | Space/hostile path quoting, rclone vs Desktop variants, templates and backend selection. |
| Packed distribution and parser | `scripts/smoke-package.mjs`; packaged subprocess | All 19 top-level command groups' help, version, invalid command/option, bundled Python/Nix/Docker sources, templates/retrieval fixture/LICENSE, version coupling and excluded tests/secrets/caches. No publication. |
| Child-process test oracle | `scripts/checked-process.mjs`, `test/checked-process.test.ts`; real child processes | Normal exit checked; actual timeout, SIGTERM and spawn failure cannot masquerade as expected nonzero completion. Finite subprocess timeouts and finally cleanup. |

## CI gates and evidence boundaries

`.github/workflows/release.yml` runs unit checks plus the offline evaluator, PostgreSQL
16/pgvector integration (including all member variants), and separate packed CLI smoke
on Linux with Node 18 (declared engine minimum) and 20. Both existing macOS Nix builds
remain: `app-fake`, `up-fake`, `up-real` on ARM and Intel. Tag publication requires
`check`, `integration`, `nix-build` and the entire `package-smoke` matrix. Smoke uses
POSIX executable stubs and is intentionally a Linux gate; it does not establish
Windows or macOS installed-CLI behavior. Nix builds establish buildability, not live
service startup or real-model relevance.

The local suite can prove SQLite behavior and integration collection without a
PostgreSQL server. PostgreSQL runtime evidence is separate: GitHub Actions run
41173070873, integration job 124454927736 passed all 37 PostgreSQL integration tests,
including 15 new member cases. PostgreSQL emits the existing SQLAlchemy
`ARRAY.Comparator.any()` deprecation warning more often with the expanded scenarios;
this work does not change production queries to suppress it.

A temporary isolated app copy changed `loops._with_author` to trust the wire author.
`test_trust_conflicts_and_exclusion_across_members[sqlite]` failed at the independent
expected `bob` folder-author assertion (actual forged `alice`); restoring the original
bytes passed the same test. The tracked source was byte-checked unchanged. A separate
repository visibility-predicate mutant survived this focused scenario because the
import layer already rejects unauthorized edges; this is not a claim that every
mutation is detected. Injected real
publication/bookkeeping/transaction failures must turn health degraded while preserving
exact state, repaired inputs/retries must return green, and abnormal child completions
must fail the harness. Bounded deterministic schedules are not random fuzzing or a
proof over every schedule. No coverage percentage substitutes for these oracles.

Remaining gaps: real Drive propagation/ACL enforcement and FUSE behavior; OAuth and
network delivery; Docker/Nix live lifecycle, host editor/OS integration, Windows/macOS
packaged smoke; real semantic-model quality; arbitrary event graphs, wall-clock races,
power loss or subprocess crash recovery. The restart case recreates connections and
services, not an OS process. Background import scheduling and HTTP lifespan shutdown
are not newly expanded. Offline dependency staging tests shipped module resolution,
not npm registry installation/authentication. Integration tests and these finite
failure injections do not claim exhaustive input coverage.
