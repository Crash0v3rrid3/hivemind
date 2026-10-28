import { afterEach, expect, it, vi } from 'vitest';
import { spawnSync } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import path from 'node:path';
import { Command } from 'commander';
import { makeTempDir, withEnv } from './helpers.js';
import { ConfigError, parseConf, resolveConfig, type RawConf } from '../src/config.js';
import { guardScriptContent, probeMcp } from '../src/guard.js';
import {
  mergeUserPromptSubmitHook,
  removeClaudeMdBlock,
  removeUserPromptSubmitHook,
  shellQuote,
} from '../src/claude.js';
import { usageBlock } from '../src/usage-block.js';
import { provisionUser } from '../src/commands/admin.js';
import { register } from '../src/commands/config.js';
const cleanup: (() => void)[] = [];
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  cleanup
    .splice(0)
    .reverse()
    .forEach((f) => f());
  process.exitCode = 0;
});
it.each([
  'embeddings = "false"',
  'roster = "alice"',
  'roster = [42]',
  'roster = ["Bad Handle"]',
  'mcp_port = "7777"',
  'mount_path = 42',
  'username = true',
])('rejects malformed TOML %s', (text) => {
  expect(() => parseConf(text)).toThrow(ConfigError);
});
it('validates direct raw objects and environment roster', () => {
  const base = {
    username: 'alice',
    gdrive_folder_url: 'https://drive.google.com/drive/folders/ABC',
  };
  expect(() => resolveConfig({ ...base, embeddings: 'false' } as unknown as RawConf)).toThrow(
    ConfigError,
  );
  expect(() => resolveConfig(base, { HIVEMIND_ROSTER: 'Bad Handle' })).toThrow(ConfigError);
});
it('preserves surrounding markdown bytes', () => {
  const before = 'Before  \n\n    code\n\n';
  const after = '\n\nAfter  \n';
  expect(removeClaudeMdBlock(before + usageBlock() + after)).toBe(before + after);
});
it('quotes literal hook paths and migrates/removes legacy hooks', () => {
  const { dir, cleanup: clean } = makeTempDir();
  cleanup.push(clean);
  const file = path.join(dir, "guard ' with $special.sh");
  writeFileSync(file, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  const legacy = { hooks: { UserPromptSubmit: [{ hooks: [{ type: 'command', command: file }] }] } };
  const merged = mergeUserPromptSubmitHook(legacy, file);
  const groups = (merged.hooks as typeof legacy.hooks).UserPromptSubmit;
  expect(groups).toHaveLength(1);
  expect(spawnSync('/bin/sh', ['-c', groups[0]!.hooks[0]!.command]).status).toBe(0);
  expect(removeUserPromptSubmitHook(merged, file)).toEqual({});
  expect(mergeUserPromptSubmitHook(merged, file)).toEqual(merged);
});
it.each([500, 404, 201])('rejects HTTP %i with readiness payload', async (status) => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response('{"service":"hivemind","status":"ok"}', { status })),
  );
  expect(await probeMcp('http://localhost:7777/mcp')).toBe(false);
});
it.each(['{}', 'not json', '{"status":"ok"}', '{"service":"hivemind","status":"starting"}'])(
  'rejects incorrect readiness %s',
  async (body) => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(body)),
    );
    expect(await probeMcp('http://localhost:7777/mcp')).toBe(false);
  },
);
it('accepts identifiable healthy service', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response('{"service":"hivemind","status":"ok"}')),
  );
  expect(await probeMcp('http://localhost:7777/mcp')).toBe(true);
});
it('executes shell hook with blocking exit 2 and bypass exit 0', () => {
  const { dir, cleanup: clean } = makeTempDir();
  cleanup.push(clean);
  const curl = path.join(dir, 'curl');
  writeFileSync(curl, '#!/bin/sh\nexit 7\n', { mode: 0o755 });
  const run = (disable: string) =>
    spawnSync('/bin/sh', ['-c', guardScriptContent()], {
      env: { ...process.env, PATH: `${dir}:${process.env.PATH}`, HIVEMIND_GUARD_DISABLE: disable },
    });
  expect(run('0').status).toBe(2);
  expect(run('1').status).toBe(0);
  writeFileSync(curl, '#!/bin/sh\nprintf \'{"service":"hivemind","status":"ok"}200\'\n', {
    mode: 0o755,
  });
  expect(run('0').status).toBe(0);
  writeFileSync(curl, '#!/bin/sh\nprintf \'{"service":"hivemind","status":"ok"}500\'\n', {
    mode: 0o755,
  });
  expect(run('0').status).toBe(2);
});
it('uses actual ticket-filter tool in instructions', () => {
  expect(usageBlock()).not.toContain('memory_search_task');
  expect(usageBlock()).toContain('jira_ticket');
});
it('opens editor with arguments and literal config path; reports missing editors', async () => {
  const { dir, cleanup: clean } = makeTempDir();
  cleanup.push(clean);
  const conf = path.join(dir, "config ' space.conf");
  writeFileSync(conf, 'username="alice"');
  const editor = path.join(dir, 'editor');
  writeFileSync(editor, '#!/bin/sh\n[ "$1" = "--wait" ] && [ -f "$2" ]\n', { mode: 0o755 });
  cleanup.push(withEnv({ HIVEMIND_CONF: conf, EDITOR: `${shellQuote(editor)} --wait` }));
  const cmd = new Command();
  register(cmd);
  await cmd.parseAsync(['config', '--edit'], { from: 'user' });
  expect(process.exitCode).toBe(0);
  process.env.EDITOR = '/nonexistent-editor';
  await cmd.parseAsync(['config', '--edit'], { from: 'user' });
  expect(process.exitCode).not.toBe(0);
});
it('prints every effective config field', async () => {
  const { dir, cleanup: clean } = makeTempDir();
  cleanup.push(clean);
  const conf = path.join(dir, 'conf');
  writeFileSync(
    conf,
    'username="alice"\ngdrive_folder_url="https://drive.google.com/drive/folders/ABC"\npersistence="sqlite"\ndeploy_backend="docker"',
  );
  cleanup.push(withEnv({ HIVEMIND_CONF: conf }));
  const log = vi.spyOn(console, 'log').mockImplementation(() => {});
  const cmd = new Command();
  register(cmd);
  await cmd.parseAsync(['config', '--print'], { from: 'user' });
  expect(JSON.parse(log.mock.calls[0]![0] as string)).toMatchObject({
    deployBackend: 'docker',
    persistence: 'sqlite',
    roster: [],
  });
});
it('reuses folder after permission failure', async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(Response.json({ files: [] }))
    .mockResolvedValueOnce(Response.json({ id: 'F', name: 'alice' }))
    .mockResolvedValueOnce(Response.json({ permissions: [] }))
    .mockResolvedValueOnce(new Response('failed', { status: 500 }))
    .mockResolvedValueOnce(Response.json({ files: [{ id: 'F', name: 'alice' }] }))
    .mockResolvedValueOnce(Response.json({ permissions: [] }))
    .mockResolvedValueOnce(Response.json({ id: 'P' }));
  vi.stubGlobal('fetch', fetch);
  await expect(provisionUser('token', 'H', 'alice', 'a@example.com')).rejects.toThrow('500');
  await expect(provisionUser('token', 'H', 'alice', 'a@example.com')).resolves.toMatchObject({
    id: 'F',
  });
  expect(
    fetch.mock.calls.filter(
      ([url, init]) => String(url).includes('fields=id,name') && init.method === 'POST',
    ),
  ).toHaveLength(1);
});
it('rejects duplicate folders across pages', async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(Response.json({ files: [{ id: 'A' }], nextPageToken: 'next' }))
    .mockResolvedValueOnce(Response.json({ files: [{ id: 'B' }] }));
  vi.stubGlobal('fetch', fetch);
  await expect(provisionUser('token', 'H', 'alice', 'a@example.com')).rejects.toThrow('Ambiguous');
  expect(fetch).toHaveBeenCalledTimes(2);
});
it('does not duplicate an existing writer permission', async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(Response.json({ files: [{ id: 'F' }] }))
    .mockResolvedValueOnce(
      Response.json({
        permissions: [{ id: 'P', type: 'user', role: 'writer', emailAddress: 'a@example.com' }],
      }),
    );
  vi.stubGlobal('fetch', fetch);
  await provisionUser('token', 'H', 'alice', 'a@example.com');
  expect(fetch).toHaveBeenCalledTimes(2);
});
it.each(['http://localhost:7777/mcp/', 'http://localhost:7777/mcp?x=1#fragment'])(
  'uses canonical health URL in both probes: %s',
  async (url) => {
    const { dir, cleanup: clean } = makeTempDir();
    cleanup.push(clean);
    writeFileSync(
      path.join(dir, 'curl'),
      '#!/bin/sh\nfor arg do last="$arg"; done\n[ "$last" = "http://localhost:7777/healthz" ] || exit 9\nprintf \'{"service":"hivemind","status":"ok"}200\'\n',
      { mode: 0o755 },
    );
    const run = spawnSync('/bin/sh', ['-c', guardScriptContent(url)], {
      env: { ...process.env, PATH: `${dir}:${process.env.PATH}`, HIVEMIND_GUARD_DISABLE: '0' },
    });
    expect(run.status).toBe(0);
    const fetch = vi.fn(async (target: URL) => {
      expect(target.pathname + target.search + target.hash).toBe('/healthz');
      return Response.json({ service: 'hivemind', status: 'ok' });
    });
    vi.stubGlobal('fetch', fetch);
    expect(await probeMcp(url)).toBe(true);
  },
);
it('refuses author identity reassignment on a bound folder', async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(
      Response.json({
        files: [
          {
            id: 'F',
            appProperties: { hivemindHandle: 'alice', hivemindMemberEmail: 'original@example.com' },
          },
        ],
      }),
    );
  vi.stubGlobal('fetch', fetch);
  await expect(provisionUser('token', 'H', 'alice', 'other@example.com')).rejects.toThrow(
    'bound to another member',
  );
  expect(fetch).toHaveBeenCalledTimes(1);
});
it('refuses adding another writer on legacy author folders', async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(Response.json({ files: [{ id: 'F' }] }))
    .mockResolvedValueOnce(
      Response.json({
        permissions: [
          { id: 'P', type: 'user', role: 'writer', emailAddress: 'original@example.com' },
        ],
      }),
    );
  vi.stubGlobal('fetch', fetch);
  await expect(provisionUser('token', 'H', 'alice', 'other@example.com')).rejects.toThrow(
    'different writer',
  );
  expect(fetch).toHaveBeenCalledTimes(2);
});
it('upgrades an existing reader without creating another permission', async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(Response.json({ files: [{ id: 'F' }] }))
    .mockResolvedValueOnce(
      Response.json({
        permissions: [{ id: 'P', type: 'user', role: 'reader', emailAddress: 'a@example.com' }],
      }),
    )
    .mockResolvedValueOnce(Response.json({ id: 'P' }));
  vi.stubGlobal('fetch', fetch);
  await provisionUser('token', 'H', 'alice', 'a@example.com');
  expect(fetch.mock.calls[2]![0]).toContain('/permissions/P');
  expect(fetch.mock.calls[2]![1]).toMatchObject({ method: 'PATCH', body: '{"role":"writer"}' });
});
