/**
 * Shared test helpers: hermetic temp directories + scoped environment overrides.
 * These keep every test off the real ~/.claude and ~/.hivemind.
 */
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

/** Create a fresh temp directory; returns its path plus a cleanup fn. */
export function makeTempDir(prefix = 'hivemind-test-'): { dir: string; cleanup: () => void } {
  const dir = mkdtempSync(path.join(os.tmpdir(), prefix));
  return {
    dir,
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  };
}

/**
 * Snapshot the given env keys, apply overrides, and return a restore fn that
 * puts every key back exactly as it was (deleting keys that were originally unset).
 */
export function withEnv(overrides: Record<string, string | undefined>): () => void {
  const keys = Object.keys(overrides);
  const saved = new Map<string, string | undefined>();
  for (const k of keys) {
    saved.set(k, process.env[k]);
    const v = overrides[k];
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  return () => {
    for (const k of keys) {
      const orig = saved.get(k);
      if (orig === undefined) delete process.env[k];
      else process.env[k] = orig;
    }
  };
}
