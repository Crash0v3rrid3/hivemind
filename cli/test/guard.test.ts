import { describe, it, expect, afterEach } from 'vitest';
import { readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import {
  DEFAULT_MCP_URL,
  guardScriptContent,
  installGuardScript,
  mcpUrlForPort,
  probeMcp,
  resolveGuardUrl,
  runGuard,
} from '../src/guard.js';
import { makeTempDir, withEnv } from './helpers.js';

const cleanups: Array<() => void> = [];
afterEach(() => {
  while (cleanups.length) cleanups.pop()!();
});

// An address that reliably fails to connect fast (reserved TEST-NET-1, discard-ish).
const UNREACHABLE_URL = 'http://127.0.0.1:1/mcp';

// ---------------------------------------------------------------------------
// mcpUrlForPort / DEFAULT_MCP_URL
// ---------------------------------------------------------------------------
describe('mcpUrlForPort', () => {
  it('builds a localhost mcp url for a port', () => {
    expect(mcpUrlForPort(7777)).toBe('http://localhost:7777/mcp');
    expect(mcpUrlForPort(9001)).toBe('http://localhost:9001/mcp');
  });

  it('default url matches port 7777', () => {
    expect(DEFAULT_MCP_URL).toBe(mcpUrlForPort(7777));
  });
});

// ---------------------------------------------------------------------------
// resolveGuardUrl — precedence env > fallback > default
// ---------------------------------------------------------------------------
describe('resolveGuardUrl', () => {
  it('returns the built-in default when nothing is set', () => {
    expect(resolveGuardUrl({})).toBe(DEFAULT_MCP_URL);
  });

  it('uses the explicit fallback over the default', () => {
    expect(resolveGuardUrl({}, 'http://localhost:9/mcp')).toBe('http://localhost:9/mcp');
  });

  it('lets HIVEMIND_MCP_URL env win over the fallback', () => {
    expect(
      resolveGuardUrl({ HIVEMIND_MCP_URL: 'http://envhost:1/mcp' }, 'http://fb:2/mcp'),
    ).toBe('http://envhost:1/mcp');
  });

  it('falls back to the default when fallback is explicitly undefined', () => {
    expect(resolveGuardUrl({}, undefined as unknown as string)).toBe(DEFAULT_MCP_URL);
  });
});

// ---------------------------------------------------------------------------
// guardScriptContent
// ---------------------------------------------------------------------------
describe('guardScriptContent', () => {
  const script = guardScriptContent();

  it('is a POSIX sh script', () => {
    expect(script.startsWith('#!/bin/sh')).toBe(true);
  });

  it('bypasses when HIVEMIND_GUARD_DISABLE=1', () => {
    expect(script).toContain('[ "$HIVEMIND_GUARD_DISABLE" = "1" ] && exit 0');
  });

  it('bakes the default url but lets HIVEMIND_MCP_URL override at runtime', () => {
    expect(script).toContain(`\${HIVEMIND_MCP_URL:-${DEFAULT_MCP_URL}}`);
  });

  it('probes with curl and a short timeout (never execs npx on the hot path)', () => {
    expect(script).toContain('curl -s -m 2 -o /dev/null "$URL"');
    expect(script).toMatch(/curl .* && exit 0/);
    // npx may only appear inside the echo suggestion message, never as a run command.
    for (const line of script.split('\n')) {
      const trimmed = line.trim();
      if (trimmed.includes('npx')) {
        expect(trimmed.startsWith('echo ')).toBe(true);
      }
    }
  });

  it('exits non-zero with a helpful message when unreachable', () => {
    expect(script).toContain('exit 1');
    expect(script).toContain('Hivemind MCP unreachable');
  });

  it('parameterises the baked-in default url', () => {
    const custom = guardScriptContent('http://localhost:9099/mcp');
    expect(custom).toContain('${HIVEMIND_MCP_URL:-http://localhost:9099/mcp}');
  });
});

// ---------------------------------------------------------------------------
// installGuardScript — hermetic temp dir
// ---------------------------------------------------------------------------
describe('installGuardScript', () => {
  it('writes guard.sh into ~/.hivemind and makes it executable', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const hmDir = path.join(dir, 'hivemind');
    const restore = withEnv({ HIVEMIND_DIR: hmDir });
    cleanups.push(restore);

    const p = installGuardScript();
    expect(p).toBe(path.join(hmDir, 'guard.sh'));
    expect(readFileSync(p, 'utf8')).toContain('#!/bin/sh');
    const mode = statSync(p).mode & 0o777;
    expect(mode).toBe(0o755);
  });

  it('bakes a custom default url into the installed script', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const restore = withEnv({ HIVEMIND_DIR: path.join(dir, 'hivemind') });
    cleanups.push(restore);

    const p = installGuardScript('http://localhost:8500/mcp');
    expect(readFileSync(p, 'utf8')).toContain(
      '${HIVEMIND_MCP_URL:-http://localhost:8500/mcp}',
    );
  });
});

// ---------------------------------------------------------------------------
// probeMcp
// ---------------------------------------------------------------------------
describe('probeMcp', () => {
  it('returns false for an unreachable endpoint', async () => {
    expect(await probeMcp(UNREACHABLE_URL, 1000)).toBe(false);
  });

  it('returns false when the request times out', async () => {
    // Route to a non-routable TEST-NET address with a tiny timeout.
    expect(await probeMcp('http://192.0.2.1:7777/mcp', 100)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// runGuard
// ---------------------------------------------------------------------------
describe('runGuard', () => {
  it('bypasses (exit 0) when HIVEMIND_GUARD_DISABLE=1', async () => {
    expect(await runGuard({ HIVEMIND_GUARD_DISABLE: '1' })).toBe(0);
  });

  it('returns 1 when the MCP endpoint is unreachable', async () => {
    const code = await runGuard({ HIVEMIND_MCP_URL: UNREACHABLE_URL });
    expect(code).toBe(1);
  });

  it('does not bypass for HIVEMIND_GUARD_DISABLE values other than "1"', async () => {
    const code = await runGuard({
      HIVEMIND_GUARD_DISABLE: '0',
      HIVEMIND_MCP_URL: UNREACHABLE_URL,
    });
    expect(code).toBe(1);
  });
});
