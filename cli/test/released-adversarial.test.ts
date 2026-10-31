import * as childProcess from 'node:child_process';
import { afterEach, expect, it, vi } from 'vitest';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { parseConf, resolveConfig, validateHandle, validatePort } from '../src/config.js';
import {
  installMcpServer,
  mergeUserPromptSubmitHook,
  removeUserPromptSubmitHook,
} from '../src/claude.js';
import { recordedBackend, clearBackendRecord, runUp } from '../src/commands/lifecycle.js';
import { makeTempDir, withEnv } from './helpers.js';
vi.mock('node:child_process', () => ({
  spawn: vi.fn(() => {
    throw new Error('unexpected external spawn');
  }),
  spawnSync: vi.fn(() => {
    throw new Error('unexpected external spawnSync');
  }),
}));
const cleanups: (() => void)[] = [];
afterEach(() => {
  vi.restoreAllMocks();
  cleanups
    .splice(0)
    .reverse()
    .forEach((f) => f());
});
function home() {
  const temp = makeTempDir();
  cleanups.push(
    temp.cleanup,
    withEnv({
      HIVEMIND_TEST_HOME: temp.dir,
      HIVEMIND_DIR: path.join(temp.dir, '.hivemind'),
      HIVEMIND_CONF: path.join(temp.dir, '.hivemind.conf'),
    }),
  );
  return temp.dir;
}
it.each(['Alice', '../alice', 'a/b', 'a\\b', 'a\n', '', ' alice', 'é'])(
  'rejects hostile author handle %j',
  (value) => {
    expect(() => validateHandle(value)).toThrow();
  },
);
it.each(['7777x', '', 0, 65536, 1.5, NaN, Infinity])('rejects invalid port %j', (value) => {
  expect(() => validatePort(value)).toThrow();
});
it.each(['username="a"\nusername="b"', 'roster=["a", false]', 'embeddings=1', 'mcp_port=true'])(
  'rejects malformed TOML %s',
  (value) => {
    expect(() => parseConf(value)).toThrow();
  },
);
it('environment overrides all configurable backend choices without mutating parsed input', () => {
  const raw = parseConf(
    'username="alice"\ngdrive_folder_url="https://drive.google.com/folders/ABC"\nmcp_port=7777',
  );
  const before = structuredClone(raw);
  const cfg = resolveConfig(raw, {
    HIVEMIND_USERNAME: 'bob',
    HIVEMIND_MCP_PORT: '1234',
    HIVEMIND_EMBEDDINGS: 'false',
    HIVEMIND_PERSISTENCE: 'sqlite',
    HIVEMIND_DEPLOY_BACKEND: 'docker',
  });
  expect(cfg).toMatchObject({
    ownerHandle: 'bob',
    mcpPort: 1234,
    embeddings: false,
    persistence: 'sqlite',
    deployBackend: 'docker',
  });
  expect(raw).toEqual(before);
});
it('refuses malformed editor JSON before overwriting sibling MCP configuration', () => {
  const dir = home();
  const file = path.join(dir, '.claude.json');
  const original = '{"mcpServers":{"sibling":';
  writeFileSync(file, original);
  expect(() => installMcpServer(7777)).toThrow();
  expect(readFileSync(file, 'utf8')).toBe(original);
});
it('hook install/uninstall preserves shared groups, metadata and unrelated events without mutation', () => {
  const command = "/tmp/a ' $ guard";
  const settings = {
    theme: 'dark',
    hooks: {
      Stop: [{ hooks: [{ type: 'command', command: 'keep' }] }],
      UserPromptSubmit: [
        {
          matcher: 'x',
          custom: 42,
          hooks: [
            { type: 'command', command },
            { type: 'command', command: 'keep' },
          ],
        },
      ],
    },
  };
  const original = structuredClone(settings);
  const installed = mergeUserPromptSubmitHook(settings, command);
  expect(mergeUserPromptSubmitHook(installed, command)).toEqual(installed);
  expect(removeUserPromptSubmitHook(installed, command)).toEqual({
    ...settings,
    hooks: {
      ...settings.hooks,
      UserPromptSubmit: [
        { matcher: 'x', custom: 42, hooks: [{ type: 'command', command: 'keep' }] },
      ],
    },
  });
  expect(settings).toEqual(original);
});
it('rejects corrupt backend state and clears only the record idempotently', () => {
  const dir = home();
  mkdirSync(path.join(dir, '.hivemind'));
  writeFileSync(path.join(dir, '.hivemind/running-backend'), 'docker; rm -rf /');
  writeFileSync(path.join(dir, '.hivemind/keep'), 'memory');
  expect(() => recordedBackend()).toThrow('refusing to guess');
  clearBackendRecord();
  clearBackendRecord();
  expect(recordedBackend()).toBeUndefined();
  expect(readFileSync(path.join(dir, '.hivemind/keep'), 'utf8')).toBe('memory');
});
it('up with absent or invalid config fails before generating local stack state', async () => {
  const dir = home();
  vi.spyOn(console, 'error').mockImplementation(() => {});
  expect(await runUp()).toBe(1);
  writeFileSync(path.join(dir, '.hivemind.conf'), 'username="INVALID"');
  expect(await runUp()).toBe(1);
  expect(() => readFileSync(path.join(dir, '.hivemind/running-backend'))).toThrow();
  expect(childProcess.spawn).not.toHaveBeenCalled();
  expect(childProcess.spawnSync).not.toHaveBeenCalled();
});
