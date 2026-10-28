/**
 * `hivemind doctor` — end-to-end health of a node:
 *   - mount reachable (own folder present)
 *   - Postgres reachable
 *   - MCP endpoint reachable
 *   - can write own folder / cannot write a peer folder (the trust guarantee)
 *   - roster is a superset of the folders present in the hive
 *   - app image tag matches the CLI version
 * Exits non-zero if any critical check fails.
 */
import { randomUUID } from 'node:crypto';
import { existsSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import type { Command } from 'commander';
import {
  cliVersion,
  ConfigError,
  hostHivePath,
  loadResolvedConfig,
  type ResolvedConfig,
} from '../config.js';
import { runComposeCapture } from '../docker.js';
import { effectiveBackend, NIX_PG_PORT, nixStateDir, tcpOpen } from '../nix.js';
import { hivemindAppDir } from '../paths.js';
import { mcpUrlForPort, probeMcp } from '../guard.js';

interface CheckResult {
  name: string;
  ok: boolean;
  critical: boolean;
  detail: string;
}

function listHiveFolders(mountPath: string): string[] {
  if (!existsSync(mountPath)) return [];
  return readdirSync(mountPath, { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => d.name);
}

function checkMount(cfg: ResolvedConfig): CheckResult {
  const own = path.join(hostHivePath(cfg), cfg.ownerHandle);
  const ok = existsSync(own) && statSync(own).isDirectory();
  return {
    name: 'mount reachable',
    ok,
    critical: true,
    detail: ok ? `own folder present at ${own}` : `own folder missing at ${own}`,
  };
}

function checkDb(): CheckResult {
  const res = runComposeCapture([
    'exec',
    '-T',
    'postgres',
    'pg_isready',
    '-U',
    'hivemind',
    '-d',
    'hivemind',
  ]);
  const ok = res.code === 0;
  return {
    name: 'postgres reachable',
    ok,
    critical: true,
    detail: ok ? 'pg_isready ok' : `pg_isready failed (${res.stderr.trim() || res.stdout.trim()})`,
  };
}

async function checkDbNix(): Promise<CheckResult> {
  const ok = await tcpOpen('127.0.0.1', NIX_PG_PORT);
  return {
    name: 'postgres reachable',
    ok,
    critical: true,
    detail: ok
      ? `listening on 127.0.0.1:${NIX_PG_PORT}`
      : `no listener on 127.0.0.1:${NIX_PG_PORT}`,
  };
}

function checkSqliteDb(): CheckResult {
  const db = path.join(nixStateDir(), 'hivemind.db');
  const ok = existsSync(db);
  return {
    name: 'sqlite db present',
    ok,
    critical: true,
    detail: ok ? `present at ${db}` : `missing ${db} — run \`hivemind up\``,
  };
}

export function checkSqliteDocker(): CheckResult {
  const res = runComposeCapture([
    'exec',
    '-T',
    'app',
    'python',
    '-c',
    "import sqlite3; c=sqlite3.connect('file:/data/hivemind.db?mode=ro', uri=True); assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok'; c.close()",
  ]);
  return {
    name: 'sqlite database readable',
    ok: res.code === 0,
    critical: true,
    detail:
      res.code === 0
        ? 'container SQLite quick_check ok'
        : `SQLite probe failed (${res.stderr.trim() || res.stdout.trim()})`,
  };
}

function checkNixStaged(): CheckResult {
  const flake = path.join(hivemindAppDir(), 'flake.nix');
  const ok = existsSync(flake);
  return {
    name: 'app flake staged',
    ok,
    critical: true,
    detail: ok ? `present at ${flake}` : `missing ${flake} — run \`hivemind setup\``,
  };
}

async function checkMcp(cfg: ResolvedConfig): Promise<CheckResult> {
  const url = mcpUrlForPort(cfg.mcpPort);
  const ok = await probeMcp(url);
  return {
    name: 'MCP reachable',
    ok,
    critical: true,
    detail: ok ? `responded at ${url}` : `no response at ${url}`,
  };
}

function checkWriteOwn(cfg: ResolvedConfig): CheckResult {
  const own = path.join(hostHivePath(cfg), cfg.ownerHandle);
  if (!existsSync(own)) {
    return {
      name: 'can write own folder',
      ok: false,
      critical: true,
      detail: 'own folder missing',
    };
  }
  const probe = path.join(own, `.hivemind-doctor-${Date.now()}`);
  try {
    writeFileSync(probe, 'ok', 'utf8');
    rmSync(probe, { force: true });
    return { name: 'can write own folder', ok: true, critical: true, detail: `wrote ${own}` };
  } catch (err) {
    return {
      name: 'can write own folder',
      ok: false,
      critical: true,
      detail: `write failed: ${(err as Error).message}`,
    };
  }
}

export function checkCannotWritePeer(cfg: ResolvedConfig): CheckResult {
  const result = { name: 'cannot write peer folders', ok: true, critical: true, detail: '' };
  let folders: string[];
  try {
    folders = listHiveFolders(hostHivePath(cfg)).filter((f) => f !== cfg.ownerHandle);
  } catch (err) {
    return { ...result, ok: false, detail: `ACL check inconclusive: ${(err as Error).message}` };
  }
  if (folders.length === 0) {
    return { ...result, critical: false, detail: 'no peer folders present to test' };
  }
  const failures: string[] = [];
  for (const peer of folders) {
    const probe = path.join(hostHivePath(cfg), peer, `.hivemind-doctor-${randomUUID()}`);
    try {
      writeFileSync(probe, 'should-fail', { encoding: 'utf8', flag: 'wx' });
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code !== 'EACCES' && code !== 'EPERM') {
        failures.push(`${peer}: inconclusive write failure (${code ?? (err as Error).message})`);
      }
      continue;
    }
    failures.push(`${peer}: write SUCCEEDED — peer folder is writable`);
    try {
      rmSync(probe);
    } catch (err) {
      failures.push(`${peer}: probe cleanup failed (${(err as Error).message}); remove ${probe}`);
    }
  }
  return {
    ...result,
    ok: failures.length === 0,
    detail: failures.length
      ? failures.join('; ')
      : `local permission denial verified for ${folders.length} peer folders; remote Drive ACLs still require verification`,
  };
}

function checkRosterSuperset(cfg: ResolvedConfig): CheckResult {
  const folders = listHiveFolders(hostHivePath(cfg));
  if (cfg.roster.length === 0) {
    return {
      name: 'roster ⊇ folders',
      ok: true,
      critical: false,
      detail: 'no explicit roster (admin roster.json governs); skipped',
    };
  }
  const rosterSet = new Set(cfg.roster);
  const unknown = folders.filter((f) => !rosterSet.has(f));
  const ok = unknown.length === 0;
  return {
    name: 'roster ⊇ folders',
    ok,
    critical: false,
    detail: ok ? 'all hive folders are in the roster' : `unknown folders: ${unknown.join(', ')}`,
  };
}

function checkImageTag(cfg: ResolvedConfig): CheckResult {
  const version = cliVersion();
  const ok = cfg.imageTag === version;
  return {
    name: 'image tag == CLI version',
    ok,
    critical: false,
    detail: ok
      ? `both ${version}`
      : `image_tag=${cfg.imageTag} but CLI version=${version} (pin them together)`,
  };
}

/** Run all doctor checks and print a report. Returns an exit code (0 = healthy). */
export async function runDoctor(): Promise<number> {
  let cfg: ResolvedConfig;
  try {
    cfg = loadResolvedConfig();
  } catch (err) {
    if (err instanceof ConfigError) {
      console.error(`config error: ${err.message}`);
      return 1;
    }
    throw err;
  }

  const { backend, note } = effectiveBackend(cfg);
  if (note) console.warn(note);
  console.log(`Deploy backend: ${backend}`);

  const results: CheckResult[] = [];
  results.push(checkMount(cfg));
  if (backend === 'nix') {
    results.push(checkNixStaged());
    results.push(cfg.persistence === 'sqlite' ? checkSqliteDb() : await checkDbNix());
  } else {
    results.push(cfg.persistence === 'sqlite' ? checkSqliteDocker() : checkDb());
  }
  results.push(await checkMcp(cfg));
  results.push(checkWriteOwn(cfg));
  results.push(checkCannotWritePeer(cfg));
  results.push(checkRosterSuperset(cfg));
  if (backend === 'docker') {
    results.push(checkImageTag(cfg));
  }

  let failedCritical = false;
  for (const r of results) {
    const mark = r.ok ? 'PASS' : r.critical ? 'FAIL' : 'WARN';
    console.log(`[${mark}] ${r.name} — ${r.detail}`);
    if (!r.ok && r.critical) failedCritical = true;
  }

  if (failedCritical) {
    console.error('\ndoctor: one or more critical checks failed.');
    return 1;
  }
  console.log('\ndoctor: healthy.');
  return 0;
}

export function register(program: Command): void {
  program
    .command('doctor')
    .description('Diagnose mount / DB / MCP / ACLs / roster / image-tag health')
    .action(async () => {
      process.exitCode = await runDoctor();
    });
}
