import { createServer, type Server } from 'node:http';
import { Command } from 'commander';
import { afterEach, expect, it, vi } from 'vitest';
import {
  fetchSyncStatus,
  register,
  runSyncStatus,
  validateSyncStatus,
} from '../src/commands/sync.js';
import { resolveConfig } from '../src/config.js';
import * as config from '../src/config.js';
const now = '2026-10-28T12:00:00+00:00';
const cycle = {
  last_attempt_at: null,
  last_success_at: null,
  last_failure_category: null,
  problem_count: 0,
  problems: [],
};
const status = {
  service: 'hivemind',
  health: 'unknown',
  telemetry_started_at: now,
  loops_enabled: true,
  remote_delivery: 'unknown',
  pending_local_exports: 0,
  quarantined_ids: 0,
  import: cycle,
  export: cycle,
  recovery_guidance: [],
};
let server: Server | undefined;
async function serve(body: unknown, code = 200, hang = false): Promise<number> {
  server = createServer((_req, res) => {
    res.writeHead(code);
    if (hang) res.write('{');
    else res.end(JSON.stringify(body));
  });
  await new Promise<void>((r) => server!.listen(0, '127.0.0.1', r));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('port');
  return address.port;
}
function configured(port: number) {
  vi.spyOn(config, 'loadResolvedConfig').mockReturnValue(
    resolveConfig({
      username: 'alice',
      gdrive_folder_url: 'https://drive.google.com/folders/test',
      mcp_port: port,
    }),
  );
}
afterEach(async () => {
  vi.restoreAllMocks();
  if (server) {
    server.closeAllConnections();
    await new Promise<void>((r) => server!.close(() => r()));
    server = undefined;
  }
});
it('preserves unknown health despite zero pending work, shows scope and remote uncertainty', async () => {
  configured(await serve(status));
  const log = vi.spyOn(console, 'log').mockImplementation(() => {});
  expect(await runSyncStatus()).toBe(0);
  expect(log.mock.calls.flat().join('\n')).toContain('Sync health: unknown');
  expect(log.mock.calls.flat().join('\n')).toContain('Remote delivery: unknown');
  expect(log.mock.calls.flat().join('\n')).toContain('Scope: this process');
});
it('prints validated JSON and exits 1 for degraded status', async () => {
  const body = { ...status, health: 'degraded', quarantined_ids: 1 };
  configured(await serve(body));
  const log = vi.spyOn(console, 'log').mockImplementation(() => {});
  expect(await runSyncStatus(true)).toBe(1);
  expect(JSON.parse(log.mock.calls[0][0] as string)).toEqual(body);
});
it('accepts completed clean cycles', () => {
  expect(
    validateSyncStatus({
      ...status,
      health: 'ok',
      import: { ...cycle, last_attempt_at: now, last_success_at: now },
      export: { ...cycle, last_attempt_at: now, last_success_at: now },
    }).health,
  ).toBe('ok');
});
it.each([
  { ...status, service: 'other' },
  { ...status, pending_local_exports: -1 },
  { ...status, remote_delivery: 'confirmed' },
  { ...status, loops_enabled: 'yes' },
  { ...status, telemetry_started_at: 'yesterday' },
  { ...status, secret: 'unexpected' },
  {
    ...status,
    import: { ...cycle, problem_count: 1, problems: [{ path: '/secret', reason: 'read' }] },
  },
  {
    ...status,
    export: {
      ...cycle,
      problem_count: 21,
      problems: Array(21).fill({ path: null, reason: 'export' }),
    },
  },
  { ...status, recovery_guidance: ['unsafe\u001b[31m'] },
])('rejects malformed response %#', (body) => {
  expect(() => validateSyncStatus(body)).toThrow('invalid sync');
});
it('rejects HTTP errors', async () => {
  await expect(fetchSyncStatus(await serve(status, 404))).rejects.toThrow();
});
it('rejects oversized bodies', async () => {
  await expect(fetchSyncStatus(await serve('x'.repeat(65537)))).rejects.toThrow();
});
it('bounds waiting for the body', async () => {
  await expect(fetchSyncStatus(await serve(null, 200, true), 30)).rejects.toThrow();
});
it('forbids redirects', async () => {
  await expect(fetchSyncStatus(await serve(status, 302))).rejects.toThrow();
});
it('reports invalid and unreachable endpoints without exception disclosure', async () => {
  configured(await serve({}));
  const error = vi.spyOn(console, 'error').mockImplementation(() => {});
  expect(await runSyncStatus()).toBe(1);
  expect(error.mock.calls[0][0]).toContain('hivemind up');
  vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('secret exception'));
  expect(await runSyncStatus()).toBe(1);
  expect(error.mock.calls.flat().join(' ')).not.toContain('secret exception');
});
it('handles config failure before HTTP', async () => {
  vi.spyOn(config, 'loadResolvedConfig').mockImplementation(() => {
    throw new Error('secret');
  });
  const fetch = vi.spyOn(globalThis, 'fetch');
  const error = vi.spyOn(console, 'error').mockImplementation(() => {});
  expect(await runSyncStatus()).toBe(1);
  expect(fetch).not.toHaveBeenCalled();
  expect(error.mock.calls[0][0]).toContain('hivemind init');
  await expect(fetchSyncStatus(0)).rejects.toThrow('invalid port');
});
it('registers sync status alongside lifecycle status', () => {
  const program = new Command();
  program.command('status');
  register(program);
  expect(program.commands.map((c) => c.name())).toEqual(['status', 'sync']);
  expect(program.commands[1].commands[0].name()).toBe('status');
});
