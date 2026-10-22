# Contributing to Hivemind

Thanks for your interest in Hivemind. This is a small, opinionated project; the
scope is deliberately narrow (see the status note in the README). Bug reports,
docs fixes, and focused improvements are welcome.

## Ground rules

- **Read [SPEC.md](./SPEC.md) first.** It is the single source of truth for the
  data model, file format, sync loops, MCP tool surface, and the invariants the
  tests assert. Build exactly to it.
- **Stay in your subtree.** `app/**` (the Python container) and `cli/**` (the
  Node/TS host CLI) have separate owners and toolchains. Keep a change inside the
  package it belongs to.
- **Never commit secrets**, and never record them in memory files. Authorship is
  derived from the filesystem, not from fields in a record.

## Project layout

| Path | What it is |
|------|------------|
| `app/` | Python container: MCP server, append-only repository, search, sync loops. |
| `cli/` | Node/TS host CLI, published to GitHub Packages. Provisions a node. |
| `docs/` | The explainer page (served via GitHub Pages). |
| `examples/` | A short end-to-end walkthrough. |
| `SPEC.md` | The specification. The source of truth. |

## Development

All targets self-bootstrap the venv / node_modules:

```sh
make check    # lint + typecheck + unit tests, both packages (must be green)
make fmt      # ruff format (app) + prettier (cli)
make lint     # ruff (app) + eslint/prettier (cli)
make typecheck
make test     # unit layer; integration tests skip without a Postgres DSN
```

- **Python (`app/`)**: ruff, `mypy --strict`, pytest (`asyncio_mode=auto`, an
  `integration` marker). Integration tests need a real Postgres and skip when
  `DATABASE_URL_TEST` is unset.
- **Node (`cli/`)**: TypeScript strict, eslint, prettier, vitest.

The CLI's file editors (CLAUDE.md block, settings merges, env + stack generation)
are pure functions by design, so they are unit-testable without touching a real
home directory.

## Submitting a change

1. Branch from `main`.
2. Keep the change surgical and in one subtree.
3. `make check` must pass.
4. Open a PR using the template; describe what and why, and note the SPEC section
   if your change touches an invariant.
