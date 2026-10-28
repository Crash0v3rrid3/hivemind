import * as childProcess from 'node:child_process';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  _resetComposeCmd,
  _resetBuildkit,
  composeUp,
  composeBaseArgs,
  dockerPullArgs,
  POSTGRES_IMAGE,
  resolveComposeCmd,
} from '../src/docker.js';

vi.mock('node:child_process', async (importOriginal) => ({
  ...await importOriginal<typeof import('node:child_process')>(),
}));

const origEnv = { ...process.env };

afterEach(() => {
  process.env = { ...origEnv };
  _resetComposeCmd();
  _resetBuildkit();
  vi.restoreAllMocks();
});

describe('composeBaseArgs', () => {
  it('returns file + env-file args scoped to HIVEMIND_DIR (no leading "compose")', () => {
    process.env.HIVEMIND_DIR = '/tmp/hv-smoke';
    const args = composeBaseArgs();
    expect(args[0]).toBe('-f');
    expect(args[1]).toContain('/tmp/hv-smoke');
    expect(args).toContain('--env-file');
    // The launcher/sub-command is resolved separately, never baked in here.
    expect(args).not.toContain('compose');
  });
});

describe('dockerPullArgs', () => {
  it('pre-pulls the pgvector postgres image by default', () => {
    expect(POSTGRES_IMAGE).toBe('pgvector/pgvector:pg16');
    expect(dockerPullArgs()).toEqual(['pull', 'pgvector/pgvector:pg16']);
  });

  it('pulls an explicitly given image', () => {
    expect(dockerPullArgs('postgres:16')).toEqual(['pull', 'postgres:16']);
  });
});

describe('resolveComposeCmd', () => {
  it('resolves to the docker plugin or the standalone binary (or null), and caches', () => {
    const first = resolveComposeCmd();
    const second = resolveComposeCmd();
    expect(second).toBe(first); // cached: same reference on repeat calls
    if (first) {
      expect(['docker', 'docker-compose']).toContain(first.cmd);
      expect(Array.isArray(first.pre)).toBe(true);
      // docker plugin form is `docker compose ...`; standalone takes no prefix.
      expect(first.cmd === 'docker' ? first.pre : []).toEqual(
        first.cmd === 'docker' ? ['compose'] : [],
      );
    }
  });
});


describe('composeUp local build', () => {
  it('builds the app before starting so changed build args cannot reuse a stale image', () => {
    const spawn = vi.spyOn(childProcess, 'spawnSync').mockReturnValue({ status: 0, stdout: '', stderr: '', pid: 1, output: [] });
    expect(composeUp()).toBe(0);
    const commands = spawn.mock.calls.map(call => (call[1] as string[]).join(' '));
    expect(commands.findIndex(command => command.endsWith('build app'))).toBeLessThan(commands.findIndex(command => command.endsWith('up -d')));
  });

  it('does not start containers after a failed local app build', () => {
    const spawn = vi.spyOn(childProcess, 'spawnSync').mockImplementation((_cmd, args) => ({ status: (args as string[]).includes('build') ? 9 : 0, stdout: '', stderr: '', pid: 1, output: [] }));
    expect(composeUp()).toBe(9);
    expect(spawn.mock.calls.some(call => (call[1] as string[]).includes('up'))).toBe(false);
  });
});
