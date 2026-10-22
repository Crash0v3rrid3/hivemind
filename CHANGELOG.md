# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Configurable persistence backend: embedded **SQLite + sqlite-vec** as an
  alternative to Postgres + pgvector, selected with `persistence` in
  `~/.hivemind.conf` (`postgres` default, or `sqlite`).
- Native **Nix** runtime (process-compose, with a devenv fallback) as the default
  `deploy_backend` on macOS, removing the hard Docker dependency.
- Project docs: explainer page under `docs/` (GitHub Pages), CONTRIBUTING,
  SECURITY, issue/PR templates, and an end-to-end example walkthrough.
- `schema_version` on every NDJSON record: an explicit, frozen-within-a-major
  on-disk format contract with forward-compatible reads (unknown keys ignored,
  a missing version treated as `1`). Documented in SPEC §5.
- CI now builds the flake on **Intel macOS** (`macos-13`, x86_64-darwin) as well
  as Apple Silicon, and runs the **Postgres integration** suite against the
  default backend before publishing.
- End-to-end cross-node convergence test proving write → export → peer-import →
  visible, folder-derived authorship, and malformed-peer-file isolation.

### Changed
- README reworked into a value-first landing page (badges, a "What you get" list,
  an honest status box) with backend-neutral persistence wording.
- **Breaking (local only):** the Postgres role/password was renamed from
  `teamtrail` to `hivemind`. Existing local Postgres volumes created under the old
  role must be re-initialized (`hivemind down` + remove the volume, then `up`);
  SQLite nodes and the NDJSON files on the mount are unaffected.

## [0.1.0]

### Added
- Initial release: serverless, append-only, semantically-searchable shared team
  memory over MCP. Per-node local stack; sync via immutable NDJSON files through a
  shared Google Drive folder; 10 MCP tools; local fastembed/onnxruntime
  embeddings. Published to GitHub Packages.
