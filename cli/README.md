# hivemind — host CLI

Provisions and operates a developer's local **Hivemind** node: a serverless,
append-only, semantically-searchable shared team memory exposed over **MCP**. Each
node runs an identical Docker Compose stack (rclone Drive mount + Postgres/pgvector
+ the app) and syncs by exchanging immutable NDJSON files through one shared Google
Drive folder.

## Install / run

Published to GitHub Packages (`@crash0v3rrid3` scope); run without a global install:

```sh
npx -y @crash0v3rrid3/hivemind <command>
```

## Quickstart

```sh
hivemind init          # scaffold ~/.hivemind.conf
hivemind config        # validate + print the resolved config
hivemind setup         # rclone OAuth + generate ~/.hivemind/{.env,rclone.conf,docker-compose.yml}
hivemind up            # start the stack (first boot ~60-90s)
hivemind doctor        # verify mount / DB / MCP / ACLs / roster / image tag
hivemind install       # one-shot: setup -> up -> doctor -> Claude integration
```

## Configuration

`~/.hivemind.conf` (TOML) is the human source of truth. Resolution precedence is
**built-in defaults < conf file < environment**. See `templates/hivemind.conf.example`.

| Key | Required | Default | Notes |
| --- | --- | --- | --- |
| `gdrive_folder_url` | yes | — | folder id extracted from `/folders/<id>` or `?id=<id>` |
| `username` | yes | — | `OWNER_HANDLE`; must match `^[a-z0-9][a-z0-9._-]*$` |
| `mcp_port` | no | `7777` | 1..65535 |
| `roster` | no | admin `roster.json` | list of member handles |
| `mount_path` | no | `/mnt/hivemind` | host FUSE mount path |
| `export_interval_sec` | no | `60` | export loop cadence |
| `import_interval_sec` | no | `60` | import loop cadence |
| `image_tag` | no | CLI version | app image tag |

Secrets never live in this file — the Drive OAuth token lives only in
`~/.hivemind/rclone.conf`.

## Commands

| Command | Purpose |
| --- | --- |
| `init` | scaffold `~/.hivemind.conf` |
| `config [--edit\|--print]` | validate/show resolved config, or edit the conf |
| `setup` | rclone remote + generate `~/.hivemind` derived files |
| `up` / `down` / `status` / `logs` | Docker Compose lifecycle (`up` regenerates first) |
| `sync status [--json]` | local pending exports, quarantine and cycle observations |
| `doctor` | health checks incl. can-write-own / cannot-write-peer |
| `install-usage-instructions` | marker block into `~/.claude/CLAUDE.md` |
| `install-mcp-server` | http entry into `~/.claude.json` |
| `install-enforcement` | `~/.hivemind/guard.sh` + `UserPromptSubmit` hook |
| `guard` | probe the MCP endpoint (exit non-zero if down) |
| `install` | one-shot of all of the above |
| `admin provision-user <handle> <email>` | create + share the member's Drive folder |


### Inspect local sync

Run `hivemind sync status` (or `npx -y @crash0v3rrid3/hivemind sync status`)
to inspect pending local exports, quarantined IDs and import/export cycle health.
Use `hivemind sync status --json` for the validated server response. The command
reads only the configured localhost MCP port, with a five-second timeout.
Degraded sync and unreachable/invalid endpoints exit 1; healthy or unknown status
exits 0. Unknown means clean cycles have not yet completed in both directions,
even if no exports are pending. Telemetry covers this app process since startup
and resets on restart. Remote delivery is always unknown: local publication
does not prove another device received it. `hivemind status` continues to show
service lifecycle status. For failures, check `hivemind logs` and the recovery
guidance displayed by sync status.

### Enforcement guard

`install-enforcement` writes a self-contained `~/.hivemind/guard.sh` (no `npx` on the
hot path) and merges a `UserPromptSubmit` hook pointing at its absolute path. The guard
probes the MCP endpoint before every prompt and blocks work when Hivemind is down.
Bypass with `HIVEMIND_GUARD_DISABLE=1`; override the url with `HIVEMIND_MCP_URL`.

## Development

```sh
npm install
npm run typecheck
npm run lint
npm test
npm run build     # tsc -> dist/, then copies templates/ into dist/
```

The idempotent editors (CLAUDE.md block, `.claude.json` merge, `settings.json` hook,
env/rclone/compose generation, folder-id extraction) are pure, exported functions
(`src/config.ts`, `src/claude.ts`, `src/generate.ts`, `src/guard.ts`) and are unit
tested without touching the real home directory (paths are overridable via env).
