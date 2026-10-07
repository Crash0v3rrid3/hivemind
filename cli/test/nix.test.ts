import { afterEach, describe, expect, it } from 'vitest';
import {
  appFlakeRef,
  effectiveBackend,
  NIX_PG_PORT,
  nixSocketPath,
  nixStateDir,
  nixUpEnv,
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
