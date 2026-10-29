/**
 * The canonical Hivemind usage-instructions text injected into ~/.claude/CLAUDE.md.
 *
 * These are markers + body only; the actual insert/replace is done by the pure
 * `upsertClaudeMdBlock` editor in claude.ts. Keeping the text here (a constant)
 * makes the block stable and diff-friendly across re-runs.
 */

/** Marker that opens the managed block. Never change once shipped. */
export const USAGE_BLOCK_BEGIN = '<!-- BEGIN HIVEMIND USAGE INSTRUCTIONS -->';
/** Marker that closes the managed block. Never change once shipped. */
export const USAGE_BLOCK_END = '<!-- END HIVEMIND USAGE INSTRUCTIONS -->';

/** The instruction body (between the markers). */
export const USAGE_BLOCK_BODY = `## Hivemind — shared team memory (MCP)

Hivemind is your engineering team's shared, append-only memory, exposed as MCP tools
(server \`hivemind\`). Everyone reads everyone's memory; your writes are automatically
attributed to you (your \`OWNER_HANDLE\`). You never pass an author and can never write
as someone else. Everything you write is visible to the team — write professionally and
**never record secrets** (passwords, tokens, keys, customer data).

### Hivemind is mandatory — do not work without it
The \`hivemind\` MCP tools are **required**. Before doing any work, prove the server is
reachable by making a real request: call \`memory_list_recent\` (e.g. \`limit=1\`) and show
the raw result. A valid response — **including an empty list** — proves the backend is
live. **If the call errors, times out, or the \`hivemind\` tools are missing, STOP — refuse
to proceed.** Do not edit files, run commands, or analyze the task. Tell the user Hivemind
is required and unreachable, and to run \`npx -y @crash0v3rrid3/hivemind up\` (first boot
takes ~60-90s), then retry.

### Search before you work
Before starting a task, call \`memory_context(task, jira_project, jira_ticket)\` to read
relevant source excerpts within an approximate token budget. This read does not write
memories automatically. Then search further so you don't redo work or re-hit a known wall:
\`memory_list_recent\` (with \`jira_ticket\`), \`memory_search\` (semantic — phrase it as the problem
you're facing), and a filtered \`memory_list_recent\` on open blockers.

### Record aggressively — this is the point of Hivemind
Capture continuously and default to recording. A normal session produces *several*
timestamped, well-tagged entries, not one summary at the end. Log the moment something
happens — a task starts, a wait begins, a blocker hits, a decision is made, Claude helps
or misfires — with the right \`event_type\`: \`update\` (task lifecycle), \`blocker\`,
\`friction\` (where efficiency is lost), \`decision\`, \`dependency\`, \`handoff\`,
\`ai_session\`, \`research\`/\`context\`/\`note\`. Put quantitative detail in \`metadata\`
(\`task_state\`, \`minutes_spent\`/\`minutes_waited\`/\`minutes_blocked\`, \`friction_type\`,
\`ai_effect\`) and cross-cutting flags in \`tags\`.

### Rules that matter
- **Always pass \`jira_project\`** (uppercase, e.g. \`BETEL\`); add \`jira_ticket\`
  (e.g. \`BETEL-123\`) whenever the work maps to one.
- **Never set or pass an author** — identity is server-enforced from your folder; any author
  in tool input (including inside \`metadata\`) is rejected.
- **Never record secrets.** Entries are stored verbatim and are visible to the whole team.
- **Append-only.** Revise with \`memory_update\` (it supersedes the old entry);
  \`memory_delete\` is a soft delete.
- Reads are team-wide; writes are yours.`;

/** The full managed block (markers + body) as inserted into CLAUDE.md. */
export function usageBlock(): string {
  return `${USAGE_BLOCK_BEGIN}\n${USAGE_BLOCK_BODY}\n${USAGE_BLOCK_END}`;
}
