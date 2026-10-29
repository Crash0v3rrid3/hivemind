/** Read-only, bounded observations from the configured local app process. */
import type { Command } from 'commander';
import { loadResolvedConfig, validatePort } from '../config.js';

const CATEGORIES = ['missing_mount', 'roster', 'read', 'parse', 'incomplete', 'import', 'export'];
const MAX_BYTES = 64 * 1024;
type ObjectValue = Record<string, unknown>;
function object(value: unknown, keys: string[]): value is ObjectValue {
  return (
    !!value &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    Object.keys(value).length === keys.length &&
    keys.every((key) => key in value)
  );
}
function count(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}
function timestamp(value: unknown): boolean {
  return (
    typeof value === 'string' &&
    /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value) &&
    Number.isFinite(Date.parse(value))
  );
}
function text(value: unknown, max: number): value is string {
  return (
    typeof value === 'string' &&
    value.length > 0 &&
    value.length <= max &&
    ![...value].some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
  );
}
function cycle(value: unknown): boolean {
  if (
    !object(value, [
      'last_attempt_at',
      'last_success_at',
      'last_failure_category',
      'problem_count',
      'problems',
    ])
  )
    return false;
  return (
    [value.last_attempt_at, value.last_success_at].every((v) => v === null || timestamp(v)) &&
    (value.last_failure_category === null ||
      CATEGORIES.includes(value.last_failure_category as string)) &&
    count(value.problem_count) &&
    Array.isArray(value.problems) &&
    value.problems.length <= 20 &&
    value.problems.length <= value.problem_count &&
    value.problems.every(
      (p: unknown) =>
        object(p, ['path', 'reason']) &&
        CATEGORIES.includes(p.reason as string) &&
        (p.path === null ||
          (text(p.path, 4096) &&
            !p.path.startsWith('/') &&
            !p.path.includes('\\') &&
            !p.path.split('/').includes('..') &&
            !/^[A-Za-z]:/.test(p.path))),
    )
  );
}
export interface SyncStatus {
  service: 'hivemind';
  health: 'unknown' | 'ok' | 'degraded';
  telemetry_started_at: string;
  loops_enabled: boolean;
  remote_delivery: 'unknown';
  pending_local_exports: number;
  quarantined_ids: number;
  import: {
    last_attempt_at: string | null;
    last_success_at: string | null;
    last_failure_category: string | null;
    problem_count: number;
    problems: { path: string | null; reason: string }[];
  };
  export: SyncStatus['import'];
  recovery_guidance: string[];
}
export function validateSyncStatus(value: unknown): SyncStatus {
  if (
    !object(value, [
      'service',
      'health',
      'telemetry_started_at',
      'loops_enabled',
      'remote_delivery',
      'pending_local_exports',
      'quarantined_ids',
      'import',
      'export',
      'recovery_guidance',
    ]) ||
    value.service !== 'hivemind' ||
    !['unknown', 'ok', 'degraded'].includes(value.health as string) ||
    !timestamp(value.telemetry_started_at) ||
    typeof value.loops_enabled !== 'boolean' ||
    value.remote_delivery !== 'unknown' ||
    !count(value.pending_local_exports) ||
    !count(value.quarantined_ids) ||
    !cycle(value.import) ||
    !cycle(value.export) ||
    !Array.isArray(value.recovery_guidance) ||
    value.recovery_guidance.length > 8 ||
    !value.recovery_guidance.every((v: unknown) => text(v, 256))
  ) {
    throw new Error('invalid sync status response');
  }
  return value as unknown as SyncStatus;
}

export async function fetchSyncStatus(port: number, timeoutMs = 5000): Promise<SyncStatus> {
  validatePort(port);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`http://127.0.0.1:${port}/sync/status`, {
      signal: controller.signal,
      redirect: 'error',
    });
    if (!response.ok || !response.body) throw new Error('invalid endpoint');
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > MAX_BYTES) {
        controller.abort();
        throw new Error('oversized response');
      }
      chunks.push(value);
    }
    return validateSyncStatus(JSON.parse(Buffer.concat(chunks).toString('utf8')));
  } finally {
    clearTimeout(timer);
  }
}

export async function runSyncStatus(json = false): Promise<number> {
  let port: number;
  try {
    port = loadResolvedConfig().mcpPort;
  } catch {
    console.error(
      'Sync status configuration is invalid or missing. Run `hivemind init` or check `hivemind config --print`.',
    );
    return 1;
  }
  let status: SyncStatus;
  try {
    status = await fetchSyncStatus(port);
  } catch {
    console.error(
      `Cannot read valid Hivemind sync status at 127.0.0.1:${port}. Run \`hivemind up\`, check \`hivemind logs\`, and ensure the app supports /sync/status.`,
    );
    return 1;
  }
  if (json) console.log(JSON.stringify(status, null, 2));
  else {
    console.log(`Sync health: ${status.health}`);
    console.log(`Pending local exports: ${status.pending_local_exports}`);
    console.log(`Quarantined IDs: ${status.quarantined_ids}`);
    console.log(
      `Scope: this process since ${status.telemetry_started_at}; loops ${status.loops_enabled ? 'enabled' : 'disabled'}`,
    );
    for (const direction of ['import', 'export'] as const) {
      const c = status[direction];
      console.log(
        `${direction}: last attempt ${c.last_attempt_at ?? 'none'}; last success ${c.last_success_at ?? 'none'}; problems ${c.problem_count}; failure ${c.last_failure_category ?? 'none'}`,
      );
    }
    console.log('Remote delivery: unknown (local publication does not confirm peer receipt)');
    for (const guidance of status.recovery_guidance) console.log(`Recovery: ${guidance}`);
  }
  return status.health === 'degraded' ? 1 : 0;
}
export function register(program: Command): void {
  program
    .command('sync')
    .description('Inspect local sync observations')
    .command('status')
    .description('Show per-process sync status (no remote delivery acknowledgement)')
    .option('--json', 'print the validated server response')
    .action(async (opts: { json?: boolean }) => {
      process.exitCode = await runSyncStatus(opts.json);
    });
}
