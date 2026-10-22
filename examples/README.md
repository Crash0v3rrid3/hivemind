# Example: a two-person hive in five minutes

This walks two teammates (alice and bob) from zero to a shared memory they can
both search. It assumes each has Node 18+, either Nix or Docker, and access to the
same shared Google Drive folder named `hivemind/`.

> See the top-level [README](../README.md) for install prerequisites (the
> `@crash0v3rrid3` GitHub Packages registry line in `~/.npmrc`) and
> [SPEC.md](../SPEC.md) for how sync actually works.

## 1. Shared folder (once, by anyone)

Create a folder `hivemind/` in a Google Drive both teammates can see, and give
each person a writable subfolder matching their handle:

```
hivemind/
  alice/     (alice: read-write, everyone else: read-only)
  bob/       (bob:   read-write, everyone else: read-only)
```

With Google Drive for Desktop, this folder syncs to a local path like
`/Users/alice/Library/CloudStorage/GoogleDrive-alice@corp.com/My Drive/hivemind`.

## 2. Each teammate provisions their node

On alice's machine:

```sh
npx -y @crash0v3rrid3/hivemind init
npx -y @crash0v3rrid3/hivemind config --edit   # username=alice, gdrive_desktop_path, mcp_port
npx -y @crash0v3rrid3/hivemind install         # generate stack, bring it up, wire Claude
npx -y @crash0v3rrid3/hivemind doctor
```

Bob does the same with `username = bob`. For a laptop with nothing else to run,
set `persistence = "sqlite"` to skip Postgres entirely.

## 3. Write a memory (alice)

Through any MCP client pointed at `http://localhost:<mcp_port>/mcp`, call
`memory_add`:

```json
{
  "event_type": "decision",
  "title": "Use sqlite persistence for laptop nodes",
  "body": "No daemon to run; the index is a single file in the state dir.",
  "jira_project": "HIVE",
  "tags": ["infra"]
}
```

Alice's export loop writes this as an immutable NDJSON file under
`hivemind/alice/<day>/`.

## 4. Read it back (bob)

Within roughly one export interval + Drive sync + one import interval, bob's import
loop picks up alice's file and stamps `author=alice` from the folder name. Bob can
now run:

```
memory_search  { "query": "what persistence do laptop nodes use" }
```

and see alice's decision, attributed to her, without either of them running a
shared server.

## What just happened

- **No central service** — each node is self-contained.
- **Sync was just files** landing in a folder both people already had.
- **Authorship came from the folder**, not from a field either client set.
- **Search was local and semantic**, using on-device embeddings carried in the files.
