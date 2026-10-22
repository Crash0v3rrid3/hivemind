# Security Policy

## Reporting a vulnerability

Please report security issues privately through GitHub's **Report a vulnerability**
flow (Security tab, Private vulnerability reporting) on this repository, rather
than opening a public issue. Include steps to reproduce and the affected version.
Expect an initial acknowledgement within a few days.

## Scope and threat model

Hivemind is serverless and runs entirely on developer machines; there is no
central service to attack. A few properties are load-bearing and worth stating:

- **Authorship is derived from the filesystem, never from the payload.** A record
  is attributed to the owner of the Drive folder it sits in. The local write path
  rejects any attempt to set `author` (including inside `metadata`), and imported
  records take their author from the read-only folder name, validated against the
  roster. A forged `author` field cannot change attribution.
- **Append-only and immutable.** NDJSON batch files are write-once; updates and
  deletes are new records. There is no in-place mutation path to exploit.
- **Trust comes from Drive permissions.** Each folder is read-write for its owner
  and read-only for everyone else. Misconfigured Drive sharing is therefore a
  security concern: keep the per-member write boundary intact.
- **No secrets in records, ever.** Do not place credentials, tokens, keys, or
  customer data in memory entries; they would sync to the whole team.

## Supported versions

This is an alpha project; only the latest release receives fixes.
