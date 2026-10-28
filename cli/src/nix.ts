/**
 * Nix (process-compose) lifecycle for the Hivemind stack — the default backend on
 * macOS. All runtime behaviour lives in the staged app flake at ~/.hivemind/app
 * (apps up|down|status|logs); this module only shells out to `nix run` with the
 * right environment, so nothing about the stack is duplicated here.
 *
 * When `nix` (with flakes) is unavailable, effectiveBackend() falls back to
 * 'docker' so a node without Nix still works.
 */
import { spawnSync } from 'node:child_process';
import net from 'node:net';
import { hiveIdentity } from './generate.js';
import path from 'node:path';
import { hostHivePath, type ResolvedConfig } from './config.js';
import { hivemindAppDir, hivemindDir } from './paths.js';

/** Fixed local Postgres port for the nix stack (avoids 5432/5433 collisions). */
export const NIX_PG_PORT = 5444;

/** The real embedder model baked by the app (matches generate.ts). */
export const EMBEDDING_MODEL = 'BAAI/bge-small-en-v1.5';
/** Sentinel selecting the deterministic FakeEmbedder. */
export const FAKE_EMBEDDING_MODEL = 'fake';

/** nix.conf may not enable flakes on the host; enable them for our invocations. */
function nixEnv(extra: NodeJS.ProcessEnv = {}): NodeJS.ProcessEnv {
  return { ...process.env, NIX_CONFIG: 'experimental-features = nix-command flakes', ...extra };
}

let _nixOk: boolean | undefined;

/** Whether `nix` (with flakes) is usable on this host. Cached after first probe. */
export function nixAvailable(): boolean {
  if (_nixOk !== undefined) return _nixOk;
  const v = spawnSync('nix', ['--version'], { stdio: 'ignore' });
  if (v.error || v.status !== 0) {
    _nixOk = false;
    return _nixOk;
  }
  const f = spawnSync('nix', ['flake', '--help'], { stdio: 'ignore', env: nixEnv() });
  _nixOk = !f.error && f.status === 0;
  return _nixOk;
}

/** Reset the cached nix probe (tests only). */
export function _resetNix(): void {
  _nixOk = undefined;
}

/**
 * The effective backend plus an optional note (the fallback reason). A config of
 * deploy_backend='nix' resolves to 'docker' when nix is absent.
 */
export function effectiveBackend(
  cfg: ResolvedConfig,
): { backend: 'nix' | 'docker'; note?: string } {
  if (cfg.deployBackend === 'docker') return { backend: 'docker' };
  if (nixAvailable()) return { backend: 'nix' };
  return { backend: 'docker', note: 'nix not found on PATH — falling back to the docker backend' };
}

/** Flake ref for an app attr in the staged app dir. PURE. */
export function appFlakeRef(attr: string): string {
  return `${hivemindAppDir()}#${attr}`;
}

/** The nix-backend state dir + unix socket (under ~/.hivemind). PURE. */
export function nixStateDir(): string {
  return path.join(hivemindDir(), 'nix-state');
}
export function nixSocketPath(): string {
  return path.join(nixStateDir(), 'pc.sock');
}

/** Env locating the state + control socket (needed by every lifecycle command). PURE. */
function nixStateEnv(): NodeJS.ProcessEnv {
  return { HIVEMIND_NIX_STATE: nixStateDir(), HIVEMIND_PC_SOCKET: nixSocketPath() };
}

/** Full runtime env for `up`, derived from the resolved config. PURE given cfg. */
export function nixUpEnv(cfg: ResolvedConfig): NodeJS.ProcessEnv {
  return {
    ...nixStateEnv(),
    HIVEMIND_HIVE: hostHivePath(cfg),
    HIVEMIND_ID: hiveIdentity(cfg),
    HIVEMIND_PGPORT: String(NIX_PG_PORT),
    HIVEMIND_PERSISTENCE: cfg.persistence,
    OWNER_HANDLE: cfg.ownerHandle,
    ROSTER: cfg.roster.join(','),
    MCP_PORT: String(cfg.mcpPort),
    EMBEDDING_MODEL: cfg.embeddings ? EMBEDDING_MODEL : FAKE_EMBEDDING_MODEL,
    EXPORT_INTERVAL_SEC: String(cfg.exportIntervalSec),
    IMPORT_INTERVAL_SEC: String(cfg.importIntervalSec),
  };
}

function runNix(args: string[], env: NodeJS.ProcessEnv): number {
  const r = spawnSync('nix', args, { stdio: 'inherit', env: nixEnv(env) });
  if (r.error) {
    const err = r.error as NodeJS.ErrnoException;
    if (err.code === 'ENOENT') {
      process.stderr.write('nix not found — install Nix (with flakes) or set deploy_backend=docker.\n');
      return 127;
    }
    process.stderr.write(`failed to run nix: ${err.message}\n`);
    return 1;
  }
  return r.status ?? 1;
}

/** `nix build <app>#up --no-link` to warm the closure during setup. */
export function nixUpAttr(cfg: Pick<ResolvedConfig, 'embeddings'>): string {
  return cfg.embeddings ? 'up-real' : 'up-fake';
}

export function nixBuildUp(cfg: Pick<ResolvedConfig, 'embeddings'> = { embeddings: false }): number {
  return runNix(['build', appFlakeRef(nixUpAttr(cfg)), '--no-link'], {});
}

/** `nix run <app>#up -- -D` (detached) with the full runtime env. */
export function nixUp(cfg: ResolvedConfig): number {
  return runNix(['run', appFlakeRef(nixUpAttr(cfg)), '--', '-D'], nixUpEnv(cfg));
}

/** `nix run <app>#down`. */
export function nixDown(): number {
  return runNix(['run', appFlakeRef('down')], nixStateEnv());
}

/** `nix run <app>#status` (process list). */
export function nixStatus(): number {
  return runNix(['run', appFlakeRef('status')], nixStateEnv());
}

/** `nix run <app>#logs -- [args]`. */
export function nixLogs(args: string[] = []): number {
  const full = ['run', appFlakeRef('logs')];
  if (args.length) full.push('--', ...args);
  return runNix(full, nixStateEnv());
}

/** TCP connectivity probe (used by doctor for the local Postgres). */
export function tcpOpen(host: string, port: number, timeoutMs = 1500): Promise<boolean> {
  return new Promise((resolve) => {
    const sock = new net.Socket();
    const done = (ok: boolean): void => {
      sock.destroy();
      resolve(ok);
    };
    sock.setTimeout(timeoutMs);
    sock.once('connect', () => done(true));
    sock.once('timeout', () => done(false));
    sock.once('error', () => done(false));
    sock.connect(port, host);
  });
}

/** Poll GET /healthz until it responds ok or attempts run out. */
export async function waitHealthy(port: number, attempts = 45): Promise<boolean> {
  for (let i = 0; i < attempts; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/healthz`, {
        signal: AbortSignal.timeout(2000),
      });
      if (res.status === 200) {
        const body = (await res.json()) as { service?: unknown; status?: unknown };
        if (body.service === 'hivemind' && body.status === 'ok') return true;
      }
    } catch {
      // retry
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  return false;
}
