import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  appFlakeRef,
  effectiveBackend,
  NIX_PG_PORT,
  nixSocketPath,
  nixStateDir,
  nixUpEnv,
  nixUpAttr,
  waitHealthy,
} from '../src/nix.js';
import { ConfigError, resolveDeployBackend, type ResolvedConfig } from '../src/config.js';

const ORIG = process.env.HIVEMIND_DIR;
afterEach(() => {
  if (ORIG === undefined) delete process.env.HIVEMIND_DIR;
  else process.env.HIVEMIND_DIR = ORIG;
});

function cfg(overrides: Partial<ResolvedConfig> = {}): ResolvedConfig {
  return {
    mountBackend: 'gdrive_desktop',
    gdriveFolderUrl: null,
    folderId: null,
    gdriveDesktopPath: '/Users/me/Drive/hivemind',
    ownerHandle: 'rishab',
    mcpPort: 7777,
    roster: ['rishab', 'sam'],
    mountPath: '/mnt/hivemind',
    exportIntervalSec: 30,
    importIntervalSec: 45,
    imageTag: '0.1.0',
    embeddings: true,
    deployBackend: 'nix',
    persistence: 'postgres',
    ...overrides,
  };
}

describe('resolveDeployBackend', () => {
  it('defaults to nix', () => expect(resolveDeployBackend(undefined)).toBe('nix'));
  it('accepts docker', () => expect(resolveDeployBackend('docker')).toBe('docker'));
  it('rejects anything else', () => expect(() => resolveDeployBackend('podman')).toThrow(ConfigError));
});

describe('nix paths', () => {
  it('derives flake ref, state dir and socket under HIVEMIND_DIR', () => {
    process.env.HIVEMIND_DIR = '/tmp/hm-test';
    expect(appFlakeRef('up')).toBe('/tmp/hm-test/app#up');
    expect(appFlakeRef('down')).toBe('/tmp/hm-test/app#down');
    expect(nixStateDir()).toBe('/tmp/hm-test/nix-state');
    expect(nixSocketPath()).toBe('/tmp/hm-test/nix-state/pc.sock');
  });
});

describe('nixUpEnv', () => {
  it('maps resolved config to the flake runtime env', () => {
    process.env.HIVEMIND_DIR = '/tmp/hm-test';
    const env = nixUpEnv(cfg());
    expect(env.HIVEMIND_HIVE).toBe('/Users/me/Drive/hivemind');
    expect(env.OWNER_HANDLE).toBe('rishab');
    expect(env.ROSTER).toBe('rishab,sam');
    expect(env.MCP_PORT).toBe('7777');
    expect(env.HIVEMIND_PGPORT).toBe(String(NIX_PG_PORT));
    expect(env.EMBEDDING_MODEL).toBe('BAAI/bge-small-en-v1.5');
    expect(env.EXPORT_INTERVAL_SEC).toBe('30');
    expect(env.IMPORT_INTERVAL_SEC).toBe('45');
    expect(env.HIVEMIND_NIX_STATE).toBe('/tmp/hm-test/nix-state');
    expect(env.HIVEMIND_PC_SOCKET).toBe('/tmp/hm-test/nix-state/pc.sock');
  });

  it('uses the fake embedder sentinel when embeddings disabled', () => {
    expect(nixUpEnv(cfg({ embeddings: false })).EMBEDDING_MODEL).toBe('fake');
  });
});

describe('effectiveBackend', () => {
  it('returns docker directly when deploy_backend=docker (no nix probe)', () => {
    expect(effectiveBackend(cfg({ deployBackend: 'docker' }))).toEqual({ backend: 'docker' });
  });

  it('resolves deploy_backend=nix to nix, or docker-with-note when nix is absent', () => {
    const r = effectiveBackend(cfg({ deployBackend: 'nix' }));
    if (r.backend === 'docker') {
      expect(r.note).toContain('nix not found');
    } else {
      expect(r.backend).toBe('nix');
      expect(r.note).toBeUndefined();
    }
  });
});


describe('Nix startup isolation', () => {
  it('selects a distinct closure for fake and real embedding modes', () => {
    expect(nixUpAttr(cfg({ embeddings: false }))).toBe('up-fake');
    expect(nixUpAttr(cfg({ embeddings: true }))).toBe('up-real');
  });

  it('keeps an empty roster empty through the generated shell assignment', () => {
    const flake = readFileSync(new URL('../../app/flake.nix', import.meta.url), 'utf8');
    const assignment = flake.split('\n').find(line => line.includes('export ROSTER='))!.trim().replaceAll("''${", '${');
    const result = spawnSync('bash', ['-c', assignment + '; printf "%s" "$ROSTER"'], {
      env: { ...process.env, OWNER: 'alice', ...nixUpEnv(cfg({ roster: [] })) }, encoding: 'utf8',
    });
    expect(result.status).toBe(0);
    expect(result.stdout).toBe('');
  });
});


describe('Nix readiness', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });
  it('rejects unrelated successful HTTP responses', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"status":"ok"}')));
    const ready = waitHealthy(7777, 1);
    await vi.runAllTimersAsync();
    expect(await ready).toBe(false);
  });
  it('accepts an identified Hivemind health response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"service":"hivemind","status":"ok"}')));
    expect(await waitHealthy(7777, 1)).toBe(true);
  });
});
