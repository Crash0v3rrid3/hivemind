/**
 * Idempotent editors for the three Claude Code config surfaces Hivemind touches:
 *   - ~/.claude/CLAUDE.md      a marker-delimited usage-instructions block
 *   - ~/.claude.json           the `mcpServers` map (add our http entry, keep siblings)
 *   - ~/.claude/settings.json  a UserPromptSubmit hook (merge, never clobber)
 *
 * The core transforms are PURE (value in -> value out). They never read the real
 * home directory, so they are fully unit-testable. Thin `install*` wrappers at the
 * bottom apply them to files.
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { claudeJsonPath, claudeMdPath, claudeSettingsPath } from './paths.js';
import { USAGE_BLOCK_BEGIN, USAGE_BLOCK_END, usageBlock } from './usage-block.js';

// ---------------------------------------------------------------------------
// CLAUDE.md marker-block upsert
// ---------------------------------------------------------------------------

/**
 * Insert or replace the managed Hivemind block in a CLAUDE.md document.
 * If both markers exist, the region between them (inclusive) is replaced in place,
 * leaving all surrounding content byte-for-byte intact. Otherwise the block is
 * appended with a blank-line separator. PURE.
 */
export function upsertClaudeMdBlock(existing: string, block: string = usageBlock()): string {
  const begin = existing.indexOf(USAGE_BLOCK_BEGIN);
  const end = existing.indexOf(USAGE_BLOCK_END);
  if (begin !== -1 && end !== -1 && end > begin) {
    const before = existing.slice(0, begin);
    const after = existing.slice(end + USAGE_BLOCK_END.length);
    return before + block + after;
  }
  if (existing.trim() === '') {
    return block + '\n';
  }
  const separator = existing.endsWith('\n') ? '\n' : '\n\n';
  return existing + separator + block + '\n';
}

// ---------------------------------------------------------------------------
// .claude.json mcpServers upsert
// ---------------------------------------------------------------------------

/** An MCP server registration for a streamable-HTTP server. */
export interface McpHttpServer {
  type: 'http';
  url: string;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/**
 * Upsert a single MCP server entry into a parsed .claude.json object without
 * disturbing any other top-level keys or any sibling servers. Returns a new object.
 * PURE.
 */
export function upsertMcpServer(
  root: Record<string, unknown>,
  name: string,
  entry: McpHttpServer,
): Record<string, unknown> {
  const next: Record<string, unknown> = { ...root };
  const servers = { ...asRecord(next.mcpServers) };
  servers[name] = entry;
  next.mcpServers = servers;
  return next;
}

/** Build the standard Hivemind MCP http entry for a given port. */
export function hivemindMcpEntry(port: number): McpHttpServer {
  return { type: 'http', url: `http://localhost:${port}/mcp` };
}

// ---------------------------------------------------------------------------
// settings.json UserPromptSubmit hook merge
// ---------------------------------------------------------------------------

interface HookCommandEntry {
  type: string;
  command: string;
  [k: string]: unknown;
}

interface HookGroup {
  matcher?: string;
  hooks?: HookCommandEntry[];
  [k: string]: unknown;
}

function groupContainsCommand(group: HookGroup, command: string): boolean {
  return Array.isArray(group.hooks) && group.hooks.some((h) => h && h.command === command);
}

/**
 * Merge a UserPromptSubmit command hook into a parsed settings.json object.
 * Idempotent: if any existing UserPromptSubmit group already runs `command`, the
 * settings are returned structurally unchanged (no duplicate group). Existing hooks
 * for this and every other event are preserved. Returns a new object. PURE.
 */
export function mergeUserPromptSubmitHook(
  settings: Record<string, unknown>,
  command: string,
): Record<string, unknown> {
  const next: Record<string, unknown> = { ...settings };
  const hooks = { ...asRecord(next.hooks) };

  const existingGroups = Array.isArray(hooks.UserPromptSubmit)
    ? (hooks.UserPromptSubmit as HookGroup[])
    : [];
  const groups: HookGroup[] = existingGroups.map((g) => ({ ...g }));

  const alreadyPresent = groups.some((g) => groupContainsCommand(g, command));
  if (!alreadyPresent) {
    groups.push({ hooks: [{ type: 'command', command }] });
  }

  hooks.UserPromptSubmit = groups;
  next.hooks = hooks;
  return next;
}

// ---------------------------------------------------------------------------
// File I/O wrappers (thin; delegate all logic to the pure editors above)
// ---------------------------------------------------------------------------

function readTextFile(p: string): string {
  return existsSync(p) ? readFileSync(p, 'utf8') : '';
}

function readJsonFile(p: string): Record<string, unknown> {
  const text = readTextFile(p);
  if (text.trim() === '') return {};
  return asRecord(JSON.parse(text));
}

function writeJsonFile(p: string, obj: Record<string, unknown>): void {
  mkdirSync(path.dirname(p), { recursive: true });
  writeFileSync(p, JSON.stringify(obj, null, 2) + '\n', 'utf8');
}

/** Install/refresh the usage-instructions block in ~/.claude/CLAUDE.md. Returns the path. */
export function installUsageInstructions(): string {
  const p = claudeMdPath();
  const existing = readTextFile(p);
  const updated = upsertClaudeMdBlock(existing);
  mkdirSync(path.dirname(p), { recursive: true });
  writeFileSync(p, updated, 'utf8');
  return p;
}

/** Upsert the Hivemind MCP http server into ~/.claude.json. Returns the path. */
export function installMcpServer(port: number): string {
  const p = claudeJsonPath();
  const root = readJsonFile(p);
  const updated = upsertMcpServer(root, 'hivemind', hivemindMcpEntry(port));
  writeJsonFile(p, updated);
  return p;
}

/** Merge the guard command into the UserPromptSubmit hook in ~/.claude/settings.json. */
export function installEnforcementHook(command: string): string {
  const p = claudeSettingsPath();
  const settings = readJsonFile(p);
  const updated = mergeUserPromptSubmitHook(settings, command);
  writeJsonFile(p, updated);
  return p;
}
