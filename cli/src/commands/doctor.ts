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
    'teamtrail',
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
    return { name: 'can write own folder', ok: false, critical: true, detail: 'own folder missing' };
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

function checkCannotWritePeer(cfg: ResolvedConfig): CheckResult {
  const folders = listHiveFolders(hostHivePath(cfg)).filter((f) => f !== cfg.ownerHandle);
  if (folders.length === 0) {
    return {
      name: 'cannot write peer folder',
      ok: true,
      critical: false,
      detail: 'no peer folders present to test',
    };
  }
  const peer = folders[0]!;
  const probe = path.join(hostHivePath(cfg), peer, `.hivemind-doctor-${Date.now()}`);
  try {
    writeFileSync(probe, 'should-fail', 'utf8');
    // If the write succeeded the trust guarantee is broken.
    rmSync(probe, { force: true });
    return {
      name: 'cannot write peer folder',
      ok: false,
      critical: true,
      detail: `WROTE into peer folder ${peer} — Drive ACLs are NOT enforcing read-only!`,
    };
  } catch {
    return {
      name: 'cannot write peer folder',
      ok: true,
      critical: true,
      detail: `write into peer folder ${peer} correctly rejected`,
    };
  }
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

  const results: CheckResult[] = [];
  results.push(checkMount(cfg));
  results.push(checkDb());
  results.push(await checkMcp(cfg));
  results.push(checkWriteOwn(cfg));
  results.push(checkCannotWritePeer(cfg));
  results.push(checkRosterSuperset(cfg));
  results.push(checkImageTag(cfg));

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
