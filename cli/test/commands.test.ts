import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Command } from 'commander';
import * as fs from 'node:fs';
import * as childProcess from 'node:child_process';
import path from 'node:path';
import * as docker from '../src/docker.js';
import * as nix from '../src/nix.js';
import * as claude from '../src/claude.js';
import * as setup from '../src/commands/setup.js';
import * as lifecycle from '../src/commands/lifecycle.js';
import * as doctor from '../src/commands/doctor.js';
import { register as registerInstall } from '../src/commands/install.js';
import { register as registerUninstall } from '../src/commands/uninstall.js';
import type { ResolvedConfig } from '../src/config.js';
import { makeTempDir, withEnv } from './helpers.js';
vi.mock('node:fs', async (original) => ({ ...(await original<typeof fs>()) }));
vi.mock('node:child_process', async (original) => ({ ...(await original<typeof childProcess>()) }));
let home: string;
let cleanup: () => void;
let restore: () => void;
let cfg: ResolvedConfig;
beforeEach(() => {
  const temp = makeTempDir();
  home = temp.dir;
  cleanup = temp.cleanup;
  restore = withEnv({ HIVEMIND_TEST_HOME: home, HIVEMIND_DIR: path.join(home, '.hivemind') });
  fs.mkdirSync(path.join(home, '.hivemind'));
  fs.mkdirSync(path.join(home, 'hive'));
  cfg = {
    mountBackend: 'gdrive_desktop',
    gdriveDesktopPath: path.join(home, 'hive'),
    gdriveFolderUrl: null,
    folderId: null,
    ownerHandle: 'me',
    mcpPort: 7777,
    roster: [],
    mountPath: home,
    exportIntervalSec: 60,
    importIntervalSec: 60,
    imageTag: '0.1.0',
    embeddings: false,
    deployBackend: 'docker',
    persistence: 'sqlite',
  };
  vi.spyOn(console, 'log').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  vi.spyOn(console, 'error').mockImplementation(() => {});
});
afterEach(() => {
  vi.restoreAllMocks();
  restore();
  cleanup();
  process.exitCode = 0;
});
function peers() {
  fs.mkdirSync(path.join(home, 'hive/alice'));
  fs.mkdirSync(path.join(home, 'hive/bob'));
}
describe('doctor ACL checks', () => {
  it('checks every peer after a permission rejection', () => {
    peers();
    const actual = fs.writeFileSync;
    vi.spyOn(fs, 'writeFileSync').mockImplementation((file, data, options) => {
      if (String(file).includes('/alice/'))
        throw Object.assign(new Error('denied'), { code: 'EACCES' });
      return actual(file, data, options);
    });
    expect(doctor.checkCannotWritePeer(cfg)).toMatchObject({
      ok: false,
      detail: expect.stringContaining('bob: write SUCCEEDED'),
    });
  });
  it('does not certify I/O errors as permission denial', () => {
    peers();
    vi.spyOn(fs, 'writeFileSync').mockImplementation(() => {
      throw Object.assign(new Error('offline'), { code: 'EIO' });
    });
    expect(doctor.checkCannotWritePeer(cfg)).toMatchObject({
      ok: false,
      detail: expect.stringContaining('inconclusive'),
    });
  });
  it('retains a writable finding when cleanup fails', () => {
    peers();
    vi.spyOn(fs, 'rmSync').mockImplementation(() => {
      throw new Error('cleanup denied');
    });
    const result = doctor.checkCannotWritePeer(cfg);
    expect(result.ok).toBe(false);
    expect(result.detail).toContain('write SUCCEEDED');
    expect(result.detail).toContain('cleanup failed');
  });
  it('reports verified local denial for all peers', () => {
    peers();
    vi.spyOn(fs, 'writeFileSync').mockImplementation(() => {
      throw Object.assign(new Error('denied'), { code: 'EPERM' });
    });
    expect(doctor.checkCannotWritePeer(cfg)).toMatchObject({
      ok: true,
      detail: expect.stringContaining('2 peer folders'),
    });
  });
});
it('probes Docker SQLite through the app container', () => {
  const capture = vi
    .spyOn(docker, 'runComposeCapture')
    .mockReturnValue({ code: 0, stdout: '', stderr: '' });
  expect(doctor.checkSqliteDocker().ok).toBe(true);
  expect(capture.mock.calls[0]?.[0]).toEqual(expect.arrayContaining(['app', 'python']));
  expect(capture.mock.calls[0]?.[0]).not.toContain('postgres');
});
it('does not activate Claude integration after critical doctor failure', async () => {
  vi.spyOn(setup, 'runSetup').mockReturnValue(0);
  vi.spyOn(lifecycle, 'runUp').mockResolvedValue(0);
  vi.spyOn(doctor, 'runDoctor').mockResolvedValue(1);
  const wire = vi.spyOn(claude, 'installUsageInstructions');
  const program = new Command();
  registerInstall(program);
  await program.parseAsync(['install'], { from: 'user' });
  expect(process.exitCode).toBe(1);
  expect(wire).not.toHaveBeenCalled();
});
describe('uninstall safety', () => {
  it('stops recorded Nix despite stale Docker configuration', async () => {
    fs.writeFileSync(path.join(home, '.hivemind/running-backend'), 'nix');
    fs.writeFileSync(path.join(home, '.hivemind.conf'), 'deploy_backend="docker"');
    const stop = vi.spyOn(nix, 'nixDown').mockReturnValue(0);
    vi.spyOn(docker, 'composeConfigExists').mockReturnValue(false);
    vi.spyOn(claude, 'uninstallEnforcementHook').mockReturnValue('settings');
    vi.spyOn(claude, 'uninstallMcpServer').mockReturnValue('mcp');
    vi.spyOn(claude, 'uninstallUsageInstructions').mockReturnValue('usage');
    const program = new Command();
    registerUninstall(program);
    await program.parseAsync(['uninstall'], { from: 'user' });
    expect(stop).toHaveBeenCalledOnce();
  });
  it.each(['nix', 'docker'] as const)(
    'retains local state when %s shutdown fails',
    async (backend) => {
      fs.writeFileSync(path.join(home, '.hivemind/running-backend'), backend);
      fs.writeFileSync(path.join(home, '.hivemind/important.db'), 'keep');
      vi.spyOn(nix, 'nixDown').mockReturnValue(1);
      vi.spyOn(docker, 'composeConfigExists').mockReturnValue(backend === 'docker');
      vi.spyOn(docker, 'composeDown').mockReturnValue(1);
      const unwire = vi.spyOn(claude, 'uninstallUsageInstructions');
      const program = new Command();
      registerUninstall(program);
      await program.parseAsync(['uninstall', '--purge'], { from: 'user' });
      expect(process.exitCode).toBe(1);
      expect(unwire).not.toHaveBeenCalled();
      expect(fs.readFileSync(path.join(home, '.hivemind/important.db'), 'utf8')).toBe('keep');
    },
  );
});
it('failed OAuth never overwrites the existing rclone config', () => {
  const configPath = path.join(home, '.hivemind/rclone.conf');
  const original = '[hivemind]\ntoken = KEEP_SECRET\nroot_folder_id = OLD\n';
  fs.writeFileSync(configPath, original);
  vi.spyOn(childProcess, 'spawnSync').mockImplementation((_command, _args, options) => {
    const temp = (options as { env?: NodeJS.ProcessEnv })?.env?.RCLONE_CONFIG;
    if (temp) fs.writeFileSync(temp, 'broken config');
    return { pid: 1, output: [], stdout: '', stderr: '', status: temp ? 1 : 0, signal: null };
  });
  expect(setup.configureRclone({ ...cfg, folderId: 'NEW' })).toBe(false);
  expect(fs.readFileSync(configPath, 'utf8')).toBe(original);
});

describe('doctor effective roster', () => {
  function adminRoster(contents: string): void {
    fs.writeFileSync(path.join(home, 'hive/roster.json'), contents);
  }

  it('uses a valid administrator roster when no explicit roster is configured', () => {
    peers();
    adminRoster('["alice", "bob"]');
    expect(doctor.checkRosterSuperset(cfg)).toMatchObject({
      ok: true,
      detail: expect.stringContaining('admin roster.json'),
    });
  });

  it('reports folders missing from the effective administrator roster', () => {
    peers();
    adminRoster('["alice"]');
    expect(doctor.checkRosterSuperset(cfg)).toMatchObject({
      ok: false,
      detail: expect.stringContaining('unknown folders: bob'),
    });
  });

  it.each(['{bad json', '[]', '{}', '[12]', '["../outside"]', '["Alice"]'])(
    'fails critically for invalid administrator roster %s',
    (contents) => {
      adminRoster(contents);
      expect(doctor.checkRosterSuperset(cfg)).toMatchObject({ ok: false, critical: true });
    },
  );

  it('lets explicit roster override even a malformed administrator roster', () => {
    peers();
    adminRoster('invalid');
    expect(doctor.checkRosterSuperset({ ...cfg, roster: ['alice', 'bob'] })).toMatchObject({
      ok: true,
      detail: expect.stringContaining('explicit roster'),
    });
  });

  it('clearly reports directory discovery fallback when no roster exists', () => {
    peers();
    expect(doctor.checkRosterSuperset(cfg)).toMatchObject({
      ok: true,
      detail: expect.stringContaining('directory discovery'),
    });
  });
});
