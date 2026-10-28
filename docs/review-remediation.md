# Repository review remediation

This change addresses review findings R01–R40 against commit
`7a25e8dce65def82dea6d613809b35051b06bd0b`.

## Wire format and coordinated upgrades

New production exports use **wire schema v2**, requiring a nonempty `embedding_model`.
This wire version is independent of the alpha application's package version. The reader
validates integer versions 1 and 2 and rejects unknown versions. V1 (including a missing
version) is parsed only as unknown-provenance legacy data; even a v1 extension named
`embedding_model` cannot supply v2 provenance. Bound repositories reject that data until
explicit migration. Low-level serialization of unknown-provenance records remains v1
for offline legacy tooling, while production writes and exports always emit v2.

Stop and upgrade **every hive member** before resuming shared writes. Mixed old/new
nodes are unsupported: older readers may ignore the schema version and accept v2 without
its provenance checks; upgraded nodes defer old v1 files. Keep immutable legacy archives,
but republish verified owner records as new v2 files using the procedure below. Do not edit
schema numbers in place or infer the model merely from vector dimensions.

## Upgrade existing nodes without losing pending writes

New databases bind their owner, hive identity and embedding model at startup.
A different owner, hive or model is rejected rather than reusing the old state.
An existing nonempty database without a binding also fails closed: old databases
cannot prove their hive or vector model automatically.

1. Stop the old application and back up its database. Retain that backup and the
   Drive archive. Do not purge the old database: pending writes may exist only there.
2. Verify the owner, hive and embedding model that produced **all** stored records.
   If records already mix fake and real vectors, restore a known-consistent backup
   or explicitly re-embed the corpus; declaring a model does not repair vectors.
3. With the new application installed, use exactly the environment generated for
   this node (`DATABASE_URL`, `OWNER_HANDLE`, `HIVEMIND_ID`, `HIVEMIND_PATH`,
   `EMBEDDING_MODEL`, `EMBEDDING_DIM`) and run:

   ```sh
   python -m hivemind_app.migrate_state --confirm-provenance --republish-own
   ```

   Run this inside the same runtime/network that can access the database. The app
   must remain stopped. This validates vector dimensions and values and local
   authorship, binds previously unbound state, and queues owner records for new
   immutable schema-v2 NDJSON files carrying explicit model provenance. It never changes
   record payloads or permits an existing binding to be reassigned.
4. Start the node, confirm successful exports, and retain backups until the other
   upgraded nodes have imported the new files. Every member with legacy files must
   republish their own records. Legacy files without model provenance are deferred;
   they are not silently treated as real-model vectors.

For a deliberate owner/hive/model change, use separate fresh state. Own-history
recovery covers exported records with known compatible model provenance. It cannot
recover local writes that were never exported. If only legacy archives remain,
restore a backed-up database or explicitly audit/migrate those archives before use.

The hive and provisioned owner directory must already exist. Startup/export no
longer creates a missing mount root. Native/desktop filesystem checks do not prove
Google Drive's remote ACLs; the administrator must maintain those permissions.

## Finding map

| Findings | Remediation |
| --- | --- |
| R01 | Docker host ports bind to loopback; document local unauthenticated trust boundary. |
| R02 | Only same-author supersession/deletion references hide records. |
| R03, R09 | Preserve empty Nix roster; load and validate administrator roster when present. |
| R04 | Import owner archive into local storage as already exported. |
| R05, R06, R10 | Validate finite float32 vectors, isolate per-file failures, defer incomplete files. |
| R07 | Bind database to owner, hive and model; explicit legacy migration. |
| R08 | Track runtime backend, stop it before purge, retain state on shutdown uncertainty. |
| R11 | Quarantine conflicting UUIDs instead of accepting whichever payload arrived first. |
| R12, R13 | Apply visibility, metadata and date restrictions before limits. |
| R14 | Preserve all authorized branches in connected revision history. |
| R15 | Explicit wire model provenance and immutable database model binding. |
| R16 | Bounded export batches and chunked SQL writes/markers. |
| R17 | Require existing hive/owner directories; disable automatic bind-source creation. |
| R18 | Move model inference and filesystem work off the event loop. |
| R19 | Probe SQLite in the Docker app container instead of requiring PostgreSQL. |
| R20, R21 | Blocking hook failure code and Hivemind-specific healthy response. |
| R22, R23 | Failed critical doctor checks block activation; inspect every peer and distinguish I/O failures. |
| R24, R25 | Explicit local builds, no application image pulls, distinct real/fake tags. |
| R26 | Preserve OAuth credentials while reconciling the remote and recreating its mount. |
| R27 | Separate Nix real/fake startup closures selected by configuration. |
| R28, R29 | Configured integration errors fail tests; Nix builds gate release publication. |
| R30 | Quote YAML literals and escape Compose interpolation in paths. |
| R31, R32 | Preserve surrounding Markdown and shell-quote installed hook paths. |
| R33 | Validate parsed configuration types, keys, roster and values. |
| R34 | Reuse unique Drive folders, bind member identity, refuse duplicates/conflicting writers. |
| R35 | Bound request sizes and strict result limits. |
| R36 | Generated instructions use the supported Jira-filtered search tool. |
| R37 | Actual MCP dispatch rejects unknown arguments, including forged author. |
| R38, R39 | Support editor arguments/errors and show complete effective configuration. |
| R40 | Exact database distance ranking avoids HNSW candidate-recall truncation. |

## Intentional tradeoffs and validation

Conflicting UUIDs are hidden on every replica, including local rows, until an
administrator investigates; no replica silently chooses a winner. Exact vector
ranking favors complete filtered results over approximate-index speed. This may
cost more on large corpora and should be benchmarked before scaling.

Regression coverage includes real SQLite persistence, full HTTP/MCP dispatch,
filesystem recovery, generated shell execution and mocked external command/API
boundaries. PostgreSQL integration and both Nix runtime builds are CI gates. Live
Docker, Nix, PostgreSQL and Google Drive behavior must be checked in their actual
runtimes; local mocked tests do not substitute for those checks.
