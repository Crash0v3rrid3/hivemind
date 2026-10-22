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

### Changed
- README reworked into a value-first landing page (badges, a "What you get" list,
  an honest status box) with backend-neutral persistence wording.

## [0.1.0]

### Added
- Initial release: serverless, append-only, semantically-searchable shared team
  memory over MCP. Per-node local stack; sync via immutable NDJSON files through a
  shared Google Drive folder; 10 MCP tools; local fastembed/onnxruntime
  embeddings. Published to GitHub Packages.
