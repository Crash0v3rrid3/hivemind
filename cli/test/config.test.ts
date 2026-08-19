import { describe, it, expect, afterEach } from 'vitest';
import { writeFileSync } from 'node:fs';
import path from 'node:path';
import {
  ConfigError,
  cliVersion,
  defaultConfig,
  extractFolderId,
  loadConf,
  parseConf,
  resolveConfig,
  validateHandle,
  validatePort,
  type ConfigDefaults,
  type RawConf,
} from '../src/config.js';
import { makeTempDir } from './helpers.js';

const DEFAULTS: ConfigDefaults = {
  mcpPort: 7777,
  mountPath: '/mnt/hivemind',
  exportIntervalSec: 60,
  importIntervalSec: 60,
  imageTag: '9.9.9',
};

const cleanups: Array<() => void> = [];
afterEach(() => {
  while (cleanups.length) cleanups.pop()!();
});

// ---------------------------------------------------------------------------
// extractFolderId
// ---------------------------------------------------------------------------
describe('extractFolderId', () => {
  it('extracts from a /folders/<id> share URL', () => {
    expect(
      extractFolderId('https://drive.google.com/drive/folders/1AbC_def-GHI123'),
    ).toBe('1AbC_def-GHI123');
  });

  it('extracts from a /folders/<id> URL with trailing query params', () => {
    expect(
      extractFolderId('https://drive.google.com/drive/folders/ABC123?usp=sharing'),
    ).toBe('ABC123');
  });

  it('extracts from a ?id=<id> URL', () => {
    expect(
      extractFolderId('https://drive.google.com/open?id=XYZ_789-abc'),
    ).toBe('XYZ_789-abc');
  });

  it('extracts from an &id=<id> URL (id not the first param)', () => {
    expect(
      extractFolderId('https://drive.google.com/open?usp=sharing&id=Mid456'),
    ).toBe('Mid456');
  });

  it('prefers the /folders/ form when both appear', () => {
    expect(
      extractFolderId('https://drive.google.com/drive/folders/FOLD1?id=SHOULD_NOT_WIN'),
    ).toBe('FOLD1');
  });

  it('rejects a URL with neither form (junk)', () => {
    expect(() => extractFolderId('https://drive.google.com/drive/my-drive')).toThrow(
      ConfigError,
    );
  });

  it('rejects a bare non-URL string', () => {
    expect(() => extractFolderId('not-a-url-at-all')).toThrow(ConfigError);
  });

  it('rejects an empty string', () => {
    expect(() => extractFolderId('')).toThrow(/empty/);
  });

  it('rejects whitespace-only input', () => {
    expect(() => extractFolderId('   ')).toThrow(ConfigError);
  });

  it('rejects a non-string input', () => {
    // @ts-expect-error deliberately passing a non-string
    expect(() => extractFolderId(undefined)).toThrow(ConfigError);
  });
});

// ---------------------------------------------------------------------------
// validateHandle
// ---------------------------------------------------------------------------
describe('validateHandle', () => {
  it.each(['alice', 'a', 'bob-smith', 'user.name', 'x_y', 'user9', '9lives'])(
    'accepts %s',
    (h) => {
      expect(validateHandle(h)).toBe(h);
    },
  );

  it.each([
    ['Alice', 'uppercase'],
    ['-alice', 'leading dash'],
    ['.alice', 'leading dot'],
    ['_alice', 'leading underscore'],
    ['user name', 'space'],
    ['user@name', 'at sign'],
    ['', 'empty'],
    ['user/name', 'slash'],
  ])('rejects %s (%s)', (h) => {
    expect(() => validateHandle(h)).toThrow(ConfigError);
  });

  it('rejects a non-string input', () => {
    // @ts-expect-error deliberately passing a non-string
    expect(() => validateHandle(42)).toThrow(ConfigError);
  });
});

// ---------------------------------------------------------------------------
// validatePort
// ---------------------------------------------------------------------------
describe('validatePort', () => {
  it('accepts an in-range integer', () => {
    expect(validatePort(7777)).toBe(7777);
  });

  it('accepts boundary ports 1 and 65535', () => {
    expect(validatePort(1)).toBe(1);
    expect(validatePort(65535)).toBe(65535);
  });

  it('coerces a numeric string', () => {
    expect(validatePort('8080')).toBe(8080);
  });

  it('rejects 0 and negatives', () => {
    expect(() => validatePort(0)).toThrow(ConfigError);
    expect(() => validatePort(-1)).toThrow(ConfigError);
  });

  it('rejects out-of-range high ports', () => {
    expect(() => validatePort(65536)).toThrow(ConfigError);
  });

  it('rejects non-integers', () => {
    expect(() => validatePort(80.5)).toThrow(ConfigError);
  });

  it('rejects non-numeric strings', () => {
    expect(() => validatePort('abc')).toThrow(ConfigError);
  });
});

// ---------------------------------------------------------------------------
// parseConf
// ---------------------------------------------------------------------------
describe('parseConf', () => {
  it('parses a full TOML config', () => {
    const raw = parseConf(
      [
        'gdrive_folder_url = "https://drive.google.com/drive/folders/ABC"',
        'username = "alice"',
        'mcp_port = 8000',
        'roster = ["alice", "bob"]',
        'mount_path = "/tmp/hive"',
        'export_interval_sec = 30',
        'import_interval_sec = 45',
        'image_tag = "1.2.3"',
      ].join('\n'),
    );
    expect(raw).toMatchObject({
      gdrive_folder_url: 'https://drive.google.com/drive/folders/ABC',
      username: 'alice',
      mcp_port: 8000,
      roster: ['alice', 'bob'],
      mount_path: '/tmp/hive',
      export_interval_sec: 30,
      import_interval_sec: 45,
      image_tag: '1.2.3',
    });
  });

  it('parses an empty document to an empty object', () => {
    expect(parseConf('')).toEqual({});
  });

  it('throws on malformed TOML', () => {
    expect(() => parseConf('this is = = not toml [[[')).toThrow();
  });
});

// ---------------------------------------------------------------------------
// resolveConfig — precedence and validation
// ---------------------------------------------------------------------------
describe('resolveConfig', () => {
  const baseRaw: RawConf = {
    gdrive_folder_url: 'https://drive.google.com/drive/folders/CONFID',
    username: 'confuser',
  };

  it('resolves a minimal conf with defaults applied', () => {
    const cfg = resolveConfig(baseRaw, {}, DEFAULTS);
    expect(cfg).toEqual({
      gdriveFolderUrl: 'https://drive.google.com/drive/folders/CONFID',
      folderId: 'CONFID',
      ownerHandle: 'confuser',
      mcpPort: 7777,
      roster: [],
      mountPath: '/mnt/hivemind',
      exportIntervalSec: 60,
      importIntervalSec: 60,
      imageTag: '9.9.9',
    });
  });

  it('derives folderId from the resolved url', () => {
    const cfg = resolveConfig(
      { ...baseRaw, gdrive_folder_url: 'https://drive.google.com/open?id=QUERYID' },
      {},
      DEFAULTS,
    );
    expect(cfg.folderId).toBe('QUERYID');
  });

  it('applies precedence defaults < conf < env for the port', () => {
    // default only
    expect(resolveConfig(baseRaw, {}, DEFAULTS).mcpPort).toBe(7777);
    // conf overrides default
    expect(resolveConfig({ ...baseRaw, mcp_port: 8001 }, {}, DEFAULTS).mcpPort).toBe(8001);
    // env overrides conf
    expect(
      resolveConfig({ ...baseRaw, mcp_port: 8001 }, { HIVEMIND_MCP_PORT: '9002' }, DEFAULTS)
        .mcpPort,
    ).toBe(9002);
  });

  it('lets env override the gdrive url (and thus folderId)', () => {
    const cfg = resolveConfig(
      baseRaw,
      { HIVEMIND_GDRIVE_FOLDER_URL: 'https://drive.google.com/drive/folders/ENVID' },
      DEFAULTS,
    );
    expect(cfg.gdriveFolderUrl).toContain('ENVID');
    expect(cfg.folderId).toBe('ENVID');
  });

  it('lets env override the username and validates it', () => {
    expect(
      resolveConfig(baseRaw, { HIVEMIND_USERNAME: 'envuser' }, DEFAULTS).ownerHandle,
    ).toBe('envuser');
    expect(() =>
      resolveConfig(baseRaw, { HIVEMIND_USERNAME: 'BadUser' }, DEFAULTS),
    ).toThrow(ConfigError);
  });

  it('resolves roster from conf array', () => {
    const cfg = resolveConfig({ ...baseRaw, roster: ['a', ' b ', ''] }, {}, DEFAULTS);
    expect(cfg.roster).toEqual(['a', 'b']);
  });

  it('resolves roster from env (comma-separated) overriding conf', () => {
    const cfg = resolveConfig(
      { ...baseRaw, roster: ['fromconf'] },
      { HIVEMIND_ROSTER: 'x, y ,z,' },
      DEFAULTS,
    );
    expect(cfg.roster).toEqual(['x', 'y', 'z']);
  });

  it('treats an empty HIVEMIND_ROSTER env as an explicit empty roster', () => {
    const cfg = resolveConfig(
      { ...baseRaw, roster: ['fromconf'] },
      { HIVEMIND_ROSTER: '' },
      DEFAULTS,
    );
    expect(cfg.roster).toEqual([]);
  });

  it('applies mount_path precedence', () => {
    expect(resolveConfig(baseRaw, {}, DEFAULTS).mountPath).toBe('/mnt/hivemind');
    expect(
      resolveConfig({ ...baseRaw, mount_path: '/conf/mnt' }, {}, DEFAULTS).mountPath,
    ).toBe('/conf/mnt');
    expect(
      resolveConfig(
        { ...baseRaw, mount_path: '/conf/mnt' },
        { HIVEMIND_MOUNT_PATH: '/env/mnt' },
        DEFAULTS,
      ).mountPath,
    ).toBe('/env/mnt');
  });

  it('applies interval precedence and validates', () => {
    expect(
      resolveConfig(
        { ...baseRaw, export_interval_sec: 15 },
        { HIVEMIND_IMPORT_INTERVAL_SEC: '25' },
        DEFAULTS,
      ),
    ).toMatchObject({ exportIntervalSec: 15, importIntervalSec: 25 });
  });

  it('rejects a non-positive interval', () => {
    expect(() =>
      resolveConfig({ ...baseRaw, export_interval_sec: 0 }, {}, DEFAULTS),
    ).toThrow(ConfigError);
    expect(() =>
      resolveConfig(baseRaw, { HIVEMIND_IMPORT_INTERVAL_SEC: '-5' }, DEFAULTS),
    ).toThrow(ConfigError);
  });

  it('applies image_tag precedence', () => {
    expect(resolveConfig(baseRaw, {}, DEFAULTS).imageTag).toBe('9.9.9');
    expect(
      resolveConfig({ ...baseRaw, image_tag: 'conf-tag' }, {}, DEFAULTS).imageTag,
    ).toBe('conf-tag');
    expect(
      resolveConfig(
        { ...baseRaw, image_tag: 'conf-tag' },
        { HIVEMIND_IMAGE_TAG: 'env-tag' },
        DEFAULTS,
      ).imageTag,
    ).toBe('env-tag');
  });

  it('throws when gdrive_folder_url is missing entirely', () => {
    expect(() => resolveConfig({ username: 'alice' }, {}, DEFAULTS)).toThrow(
      /gdrive_folder_url is required/,
    );
  });

  it('throws when username is missing entirely', () => {
    expect(() =>
      resolveConfig(
        { gdrive_folder_url: 'https://drive.google.com/drive/folders/X' },
        {},
        DEFAULTS,
      ),
    ).toThrow(/username is required/);
  });

  it('throws when the resolved url is junk', () => {
    expect(() =>
      resolveConfig({ gdrive_folder_url: 'garbage', username: 'alice' }, {}, DEFAULTS),
    ).toThrow(ConfigError);
  });

  it('throws when the resolved port is invalid', () => {
    expect(() =>
      resolveConfig(baseRaw, { HIVEMIND_MCP_PORT: '99999' }, DEFAULTS),
    ).toThrow(ConfigError);
  });
});

// ---------------------------------------------------------------------------
// loadConf — filesystem edge
// ---------------------------------------------------------------------------
describe('loadConf', () => {
  it('returns an empty conf when the file does not exist', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    expect(loadConf(path.join(dir, 'nope.conf'))).toEqual({});
  });

  it('reads and parses an existing conf file', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const p = path.join(dir, 'hivemind.conf');
    writeFileSync(p, 'username = "diskuser"\nmcp_port = 5555\n', 'utf8');
    expect(loadConf(p)).toMatchObject({ username: 'diskuser', mcp_port: 5555 });
  });
});

// ---------------------------------------------------------------------------
// defaultConfig / cliVersion
// ---------------------------------------------------------------------------
describe('defaultConfig', () => {
  it('returns the built-in defaults with a version-pinned image tag', () => {
    const d = defaultConfig();
    expect(d.mcpPort).toBe(7777);
    expect(d.mountPath).toBe('/mnt/hivemind');
    expect(d.exportIntervalSec).toBe(60);
    expect(d.importIntervalSec).toBe(60);
    expect(typeof d.imageTag).toBe('string');
    expect(d.imageTag.length).toBeGreaterThan(0);
  });
});

describe('cliVersion', () => {
  it('returns a non-empty version string', () => {
    expect(typeof cliVersion()).toBe('string');
    expect(cliVersion().length).toBeGreaterThan(0);
  });
});
