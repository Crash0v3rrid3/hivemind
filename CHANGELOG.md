# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### 0.3.0
- Explicit keyword and hybrid retrieval alongside the semantic default.
- Bounded source context packs and local sync health/recovery observations.
- Offline SQLite retrieval evaluation with explicit relevance labels, ranking
  metrics, context budgets, exact-identifier regression gates and visibility checks.
- Coupled app, CLI, runtime and template versions prepared for 0.3.0.
- Wire schema remains v2. No release tag or publication is performed here.

## [0.2.0]

### Upgrade requirements
- Stop and upgrade every hive member together. Production exports now use wire
  schema v2 with required model provenance; mixed 0.1.x/0.2.x nodes are unsupported.
- Back up existing databases, including pending local writes, and follow the
  [provenance migration guide](docs/review-remediation.md) before restarting.
  Retain legacy archives and republish verified owner records as new v2 files.

### Added
- Configurable persistence backend: embedded **SQLite + sqlite-vec** as an
  alternative to Postgres + pgvector, selected with `persistence` in
  `~/.hivemind.conf` (`postgres` default, or `sqlite`).
- Native **Nix** runtime (process-compose, with a devenv fallback) as the default
  `deploy_backend` on macOS, removing the hard Docker dependency.
- Project docs: explainer page under `docs/` (GitHub Pages), CONTRIBUTING,
  SECURITY, issue/PR templates, and an end-to-end example walkthrough.
- Explicit NDJSON wire schema versions, independent of package versions. V2
  requires model provenance; v1 and missing-version records are legacy data
  requiring explicit migration. Unknown versions are rejected. See SPEC §5.
- CI now builds the flake on **Intel macOS** (`macos-15-intel`, x86_64-darwin) as well
  as Apple Silicon, and runs the **Postgres integration** suite against the
  default backend before publishing.
- End-to-end cross-node convergence test proving write → export → peer-import →
  visible, folder-derived authorship, and malformed-peer-file isolation.

### Changed
- README reworked into a value-first landing page (badges, a "What you get" list,
  an honest status box) with backend-neutral persistence wording.
- **Breaking (local only):** the Postgres role/password was renamed from
  `teamtrail` to `hivemind`. Existing local Postgres volumes created under the old
  role require an explicit migration. Back up the database and confirm all local
  records have exported before changing roles or replacing storage. Do not delete
  the old volume while it contains pending local writes. SQLite nodes and the
  NDJSON files on the mount are unaffected.

### Fixed
- Follow-up review: reject inherited conflicting Drive writers, validate the effective
  administrator roster in doctor, and publish quarantined local evidence so replicas
  converge. The provenance-required wire format is explicitly schema v2.
- CI uses supported macOS 15 ARM/Intel runners and the current pinned Nix installer.
  Intel uses a supported Nixpkgs Darwin pin with targeted dependency metadata and
  test fixes. Model cache metadata is canonicalized with verified per-platform hashes.
- Review hardening across sync integrity, identity binding, query correctness, MCP
  validation, CLI activation/shutdown, deployment and release gates. See the
  [R01–R40 remediation and upgrade guide](docs/review-remediation.md). Existing
  nonempty databases require explicit provenance migration; back them up first.
- `make typecheck` now runs mypy against `app/pyproject.toml` (via `--config-file`)
  so it applies the intended `--strict` config and the third-party import-ignore
  overrides; previously, invoked from the repo root, mypy silently fell back to its
  default config. Pinned mypy to `<2` and resolved the type errors this surfaced in
  the test suite, so `make check` is deterministic and green across mypy versions.

## [0.1.0]

### Added
- Initial release: serverless, append-only, semantically-searchable shared team
  memory over MCP. Per-node local stack; sync via immutable NDJSON files through a
  shared Google Drive folder; 10 MCP tools; local fastembed/onnxruntime
  embeddings. Published to GitHub Packages.
