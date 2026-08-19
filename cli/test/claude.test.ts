import { describe, it, expect, afterEach } from 'vitest';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import {
  hivemindMcpEntry,
  installEnforcementHook,
  installMcpServer,
  installUsageInstructions,
  mergeUserPromptSubmitHook,
  upsertClaudeMdBlock,
  upsertMcpServer,
} from '../src/claude.js';
import { USAGE_BLOCK_BEGIN, USAGE_BLOCK_END, usageBlock } from '../src/usage-block.js';
import { makeTempDir, withEnv } from './helpers.js';

const cleanups: Array<() => void> = [];
afterEach(() => {
  while (cleanups.length) cleanups.pop()!();
});

// ---------------------------------------------------------------------------
// upsertClaudeMdBlock
// ---------------------------------------------------------------------------
describe('upsertClaudeMdBlock', () => {
  it('inserts the block into an empty document', () => {
    const out = upsertClaudeMdBlock('');
    expect(out).toContain(USAGE_BLOCK_BEGIN);
    expect(out).toContain(USAGE_BLOCK_END);
    expect(out.endsWith('\n')).toBe(true);
  });

  it('appends the block to a non-empty document, preserving existing content', () => {
    const existing = '# My notes\n\nSome important content.\n';
    const out = upsertClaudeMdBlock(existing);
    expect(out.startsWith(existing)).toBe(true);
    expect(out).toContain(USAGE_BLOCK_BEGIN);
    expect(out).toContain(USAGE_BLOCK_END);
  });

  it('appends with a separating blank line when existing has no trailing newline', () => {
    const existing = '# My notes';
    const out = upsertClaudeMdBlock(existing);
    expect(out.startsWith('# My notes\n\n')).toBe(true);
  });

  it('replaces the managed region in place, leaving surrounding content byte-for-byte intact', () => {
    const before = '# Top\n\npreamble line\n\n';
    const after = '\n\n# Bottom\n\ntrailing content\n';
    const original = `${before}${USAGE_BLOCK_BEGIN}\nOLD STALE BODY\n${USAGE_BLOCK_END}${after}`;
    const out = upsertClaudeMdBlock(original, usageBlock());
    expect(out.startsWith(before)).toBe(true);
    expect(out.endsWith(after)).toBe(true);
    expect(out).not.toContain('OLD STALE BODY');
    expect(out).toContain(usageBlock());
  });

  it('is idempotent — re-running yields identical output', () => {
    const existing = '# Notes\n\nkeep me\n';
    const once = upsertClaudeMdBlock(existing);
    const twice = upsertClaudeMdBlock(once);
    expect(twice).toBe(once);
  });

  it('accepts a custom block argument', () => {
    const custom = `${USAGE_BLOCK_BEGIN}\ncustom body\n${USAGE_BLOCK_END}`;
    const out = upsertClaudeMdBlock('', custom);
    expect(out).toContain('custom body');
  });
});

// ---------------------------------------------------------------------------
// upsertMcpServer / hivemindMcpEntry
// ---------------------------------------------------------------------------
describe('hivemindMcpEntry', () => {
  it('builds an http entry for the given port', () => {
    expect(hivemindMcpEntry(7777)).toEqual({
      type: 'http',
      url: 'http://localhost:7777/mcp',
    });
  });
});

describe('upsertMcpServer', () => {
  it('adds the entry when mcpServers is absent', () => {
    const out = upsertMcpServer({}, 'hivemind', hivemindMcpEntry(7777));
    expect(out.mcpServers).toEqual({
      hivemind: { type: 'http', url: 'http://localhost:7777/mcp' },
    });
  });

  it('preserves sibling servers', () => {
    const root = {
      mcpServers: {
        other: { type: 'stdio', command: 'foo' },
      },
    };
    const out = upsertMcpServer(root, 'hivemind', hivemindMcpEntry(7777));
    const servers = out.mcpServers as Record<string, unknown>;
    expect(servers.other).toEqual({ type: 'stdio', command: 'foo' });
    expect(servers.hivemind).toEqual({ type: 'http', url: 'http://localhost:7777/mcp' });
  });

  it('preserves other top-level keys', () => {
    const root = { numStartups: 3, theme: 'dark', mcpServers: {} };
    const out = upsertMcpServer(root, 'hivemind', hivemindMcpEntry(8888));
    expect(out.numStartups).toBe(3);
    expect(out.theme).toBe('dark');
  });

  it('overwrites an existing hivemind entry (upsert)', () => {
    const root = {
      mcpServers: { hivemind: { type: 'http', url: 'http://localhost:1/mcp' } },
    };
    const out = upsertMcpServer(root, 'hivemind', hivemindMcpEntry(9999));
    expect((out.mcpServers as Record<string, unknown>).hivemind).toEqual({
      type: 'http',
      url: 'http://localhost:9999/mcp',
    });
  });

  it('does not mutate the input object', () => {
    const root = { mcpServers: { other: { type: 'stdio' } } };
    const snapshot = JSON.stringify(root);
    upsertMcpServer(root, 'hivemind', hivemindMcpEntry(7777));
    expect(JSON.stringify(root)).toBe(snapshot);
  });

  it('treats a non-object mcpServers value defensively', () => {
    const root = { mcpServers: 'garbage' } as unknown as Record<string, unknown>;
    const out = upsertMcpServer(root, 'hivemind', hivemindMcpEntry(7777));
    expect(out.mcpServers).toEqual({
      hivemind: { type: 'http', url: 'http://localhost:7777/mcp' },
    });
  });
});

// ---------------------------------------------------------------------------
// mergeUserPromptSubmitHook
// ---------------------------------------------------------------------------
const GUARD_CMD = 'sh ~/.hivemind/guard.sh';

describe('mergeUserPromptSubmitHook', () => {
  it('adds a UserPromptSubmit group when hooks is absent', () => {
    const out = mergeUserPromptSubmitHook({}, GUARD_CMD);
    const hooks = out.hooks as Record<string, unknown>;
    const groups = hooks.UserPromptSubmit as Array<Record<string, unknown>>;
    expect(groups).toHaveLength(1);
    expect(groups[0]!.hooks).toEqual([{ type: 'command', command: GUARD_CMD }]);
  });

  it('is idempotent — re-running does not add a duplicate group', () => {
    const once = mergeUserPromptSubmitHook({}, GUARD_CMD);
    const twice = mergeUserPromptSubmitHook(once, GUARD_CMD);
    const groups = (twice.hooks as Record<string, unknown>)
      .UserPromptSubmit as unknown[];
    expect(groups).toHaveLength(1);
    expect(twice).toEqual(once);
  });

  it('preserves existing UserPromptSubmit groups that run other commands', () => {
    const existing = {
      hooks: {
        UserPromptSubmit: [
          { hooks: [{ type: 'command', command: 'other-guard.sh' }] },
        ],
      },
    };
    const out = mergeUserPromptSubmitHook(existing, GUARD_CMD);
    const groups = (out.hooks as Record<string, unknown>)
      .UserPromptSubmit as Array<Record<string, unknown>>;
    expect(groups).toHaveLength(2);
    expect(groups[0]!.hooks).toEqual([{ type: 'command', command: 'other-guard.sh' }]);
    expect(groups[1]!.hooks).toEqual([{ type: 'command', command: GUARD_CMD }]);
  });

  it('does not add a duplicate when the command already exists in a matcher group', () => {
    const existing = {
      hooks: {
        UserPromptSubmit: [
          { matcher: '*', hooks: [{ type: 'command', command: GUARD_CMD }] },
        ],
      },
    };
    const out = mergeUserPromptSubmitHook(existing, GUARD_CMD);
    const groups = (out.hooks as Record<string, unknown>).UserPromptSubmit as unknown[];
    expect(groups).toHaveLength(1);
  });

  it('preserves hooks for other events', () => {
    const existing = {
      hooks: {
        PostToolUse: [{ hooks: [{ type: 'command', command: 'post.sh' }] }],
      },
    };
    const out = mergeUserPromptSubmitHook(existing, GUARD_CMD);
    const hooks = out.hooks as Record<string, unknown>;
    expect(hooks.PostToolUse).toEqual([
      { hooks: [{ type: 'command', command: 'post.sh' }] },
    ]);
    expect((hooks.UserPromptSubmit as unknown[]).length).toBe(1);
  });

  it('preserves other top-level settings keys', () => {
    const existing = { model: 'opus', hooks: {} };
    const out = mergeUserPromptSubmitHook(existing, GUARD_CMD);
    expect(out.model).toBe('opus');
  });

  it('does not mutate the input settings', () => {
    const existing = { hooks: { UserPromptSubmit: [] } };
    const snapshot = JSON.stringify(existing);
    mergeUserPromptSubmitHook(existing, GUARD_CMD);
    expect(JSON.stringify(existing)).toBe(snapshot);
  });
});

// ---------------------------------------------------------------------------
// install* wrappers — hermetic, redirected at a temp home
// ---------------------------------------------------------------------------
describe('installUsageInstructions (file wrapper)', () => {
  it('creates ~/.claude/CLAUDE.md with the block when none exists', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const claudeDir = path.join(dir, 'claude');
    const restore = withEnv({ HIVEMIND_CLAUDE_DIR: claudeDir });
    cleanups.push(restore);

    const p = installUsageInstructions();
    expect(p).toBe(path.join(claudeDir, 'CLAUDE.md'));
    const content = readFileSync(p, 'utf8');
    expect(content).toContain(USAGE_BLOCK_BEGIN);
    expect(content).toContain(USAGE_BLOCK_END);
  });

  it('replaces the block in place, preserving surrounding CLAUDE.md content', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const claudeDir = path.join(dir, 'claude');
    mkdirSync(claudeDir, { recursive: true });
    const p = path.join(claudeDir, 'CLAUDE.md');
    writeFileSync(
      p,
      `# Personal\n\nkeep this\n\n${USAGE_BLOCK_BEGIN}\nSTALE\n${USAGE_BLOCK_END}\n`,
      'utf8',
    );
    const restore = withEnv({ HIVEMIND_CLAUDE_DIR: claudeDir });
    cleanups.push(restore);

    installUsageInstructions();
    const content = readFileSync(p, 'utf8');
    expect(content).toContain('# Personal');
    expect(content).toContain('keep this');
    expect(content).not.toContain('STALE');
  });
});

describe('installMcpServer (file wrapper)', () => {
  it('creates ~/.claude.json with the hivemind server', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const jsonPath = path.join(dir, 'claude.json');
    const restore = withEnv({ HIVEMIND_CLAUDE_JSON: jsonPath });
    cleanups.push(restore);

    const p = installMcpServer(7777);
    expect(p).toBe(jsonPath);
    const parsed = JSON.parse(readFileSync(p, 'utf8'));
    expect(parsed.mcpServers.hivemind).toEqual({
      type: 'http',
      url: 'http://localhost:7777/mcp',
    });
  });

  it('preserves sibling servers and other keys in an existing file', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const jsonPath = path.join(dir, 'claude.json');
    writeFileSync(
      jsonPath,
      JSON.stringify({
        numStartups: 5,
        mcpServers: { other: { type: 'stdio', command: 'x' } },
      }),
      'utf8',
    );
    const restore = withEnv({ HIVEMIND_CLAUDE_JSON: jsonPath });
    cleanups.push(restore);

    installMcpServer(8080);
    const parsed = JSON.parse(readFileSync(jsonPath, 'utf8'));
    expect(parsed.numStartups).toBe(5);
    expect(parsed.mcpServers.other).toEqual({ type: 'stdio', command: 'x' });
    expect(parsed.mcpServers.hivemind.url).toBe('http://localhost:8080/mcp');
  });
});

describe('installEnforcementHook (file wrapper)', () => {
  it('creates settings.json with the UserPromptSubmit hook', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const claudeDir = path.join(dir, 'claude');
    const restore = withEnv({ HIVEMIND_CLAUDE_DIR: claudeDir });
    cleanups.push(restore);

    const p = installEnforcementHook(GUARD_CMD);
    expect(p).toBe(path.join(claudeDir, 'settings.json'));
    const parsed = JSON.parse(readFileSync(p, 'utf8'));
    expect(parsed.hooks.UserPromptSubmit[0].hooks[0].command).toBe(GUARD_CMD);
  });

  it('is idempotent across repeated installs (no duplicate group)', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const claudeDir = path.join(dir, 'claude');
    const restore = withEnv({ HIVEMIND_CLAUDE_DIR: claudeDir });
    cleanups.push(restore);

    installEnforcementHook(GUARD_CMD);
    installEnforcementHook(GUARD_CMD);
    const parsed = JSON.parse(
      readFileSync(path.join(claudeDir, 'settings.json'), 'utf8'),
    );
    expect(parsed.hooks.UserPromptSubmit).toHaveLength(1);
  });

  it('preserves pre-existing hooks in settings.json', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const claudeDir = path.join(dir, 'claude');
    mkdirSync(claudeDir, { recursive: true });
    writeFileSync(
      path.join(claudeDir, 'settings.json'),
      JSON.stringify({
        model: 'opus',
        hooks: { PostToolUse: [{ hooks: [{ type: 'command', command: 'p.sh' }] }] },
      }),
      'utf8',
    );
    const restore = withEnv({ HIVEMIND_CLAUDE_DIR: claudeDir });
    cleanups.push(restore);

    installEnforcementHook(GUARD_CMD);
    const parsed = JSON.parse(
      readFileSync(path.join(claudeDir, 'settings.json'), 'utf8'),
    );
    expect(parsed.model).toBe('opus');
    expect(parsed.hooks.PostToolUse[0].hooks[0].command).toBe('p.sh');
    expect(parsed.hooks.UserPromptSubmit[0].hooks[0].command).toBe(GUARD_CMD);
  });
});
