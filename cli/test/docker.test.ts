import { afterEach, describe, expect, it } from 'vitest';

import {
  _resetComposeCmd,
  composeBaseArgs,
  dockerPullArgs,
  POSTGRES_IMAGE,
  resolveComposeCmd,
} from '../src/docker.js';

const origEnv = { ...process.env };

afterEach(() => {
  process.env = { ...origEnv };
  _resetComposeCmd();
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
