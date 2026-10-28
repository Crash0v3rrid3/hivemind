import { describe, it, expect, afterEach } from 'vitest';
import { readFileSync, statSync, existsSync, mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import {
  APP_IMAGE_NAME,
  appImageTag,
  composeScalar,
  reconcileRcloneConf,
  hiveIdentity,
  CONTAINER_HIVEMIND_PATH,
  DATABASE_URL,
  EMBEDDING_DIM,
  EMBEDDING_MODEL,
  FAKE_EMBEDDING_MODEL,
  renderDockerCompose,
  renderEnvFile,
  renderRcloneConf,
  stageAppSource,
  writeGeneratedFiles,
} from '../src/generate.js';
import type { ResolvedConfig } from '../src/config.js';
import { makeTempDir, withEnv } from './helpers.js';

const cleanups: Array<() => void> = [];
afterEach(() => {
  while (cleanups.length) cleanups.pop()!();
});

const CFG: ResolvedConfig = {
  mountBackend: 'rclone',
  gdriveFolderUrl: 'https://drive.google.com/drive/folders/FID123',
  folderId: 'FID123',
  gdriveDesktopPath: null,
  ownerHandle: 'alice',
  mcpPort: 8123,
  roster: ['alice', 'bob', 'carol'],
  mountPath: '/home/alice/hive-mnt',
  exportIntervalSec: 30,
  importIntervalSec: 45,
  imageTag: '2.3.4',
  embeddings: true,
  persistence: 'postgres',
};

const DESKTOP_CFG: ResolvedConfig = {
  ...CFG,
  mountBackend: 'gdrive_desktop',
  gdriveFolderUrl: null,
  folderId: null,
  gdriveDesktopPath: '/Users/alice/My Drive/hivemind',
};

// ---------------------------------------------------------------------------
// renderEnvFile
// ---------------------------------------------------------------------------
describe('renderEnvFile', () => {
  const env = renderEnvFile(CFG);

  it('emits the owner handle', () => {
    expect(env).toContain('OWNER_HANDLE=alice');
  });

  it('joins the roster with commas', () => {
    expect(env).toContain('ROSTER=alice,bob,carol');
  });

  it('emits an empty ROSTER value when the roster is empty', () => {
    expect(renderEnvFile({ ...CFG, roster: [] })).toContain('ROSTER=\n');
  });

  it('pins the in-container mount path (not the host mount)', () => {
    expect(env).toContain(`HIVEMIND_PATH=${CONTAINER_HIVEMIND_PATH}`);
    expect(env).not.toContain(CFG.mountPath);
  });

  it('emits the fixed database, model and dim constants', () => {
    expect(env).toContain(`DATABASE_URL=${DATABASE_URL}`);
    expect(env).toContain(`EMBEDDING_MODEL=${EMBEDDING_MODEL}`);
    expect(env).toContain(`EMBEDDING_DIM=${EMBEDDING_DIM}`);
  });

  it('emits the resolved intervals and port', () => {
    expect(env).toContain('EXPORT_INTERVAL_SEC=30');
    expect(env).toContain('IMPORT_INTERVAL_SEC=45');
    expect(env).toContain('MCP_PORT=8123');
  });

  it('binds MCP_HOST to all interfaces', () => {
    expect(env).toContain('MCP_HOST=0.0.0.0');
  });

  it('ends with a trailing newline', () => {
    expect(env.endsWith('\n')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// renderRcloneConf
// ---------------------------------------------------------------------------
describe('renderRcloneConf', () => {
  it('scopes the remote to the folder id via root_folder_id', () => {
    expect(renderRcloneConf(CFG)).toContain('root_folder_id = FID123');
  });

  it('declares the [hivemind] drive remote', () => {
    const out = renderRcloneConf(CFG);
    expect(out).toContain('[hivemind]');
    expect(out).toContain('type = drive');
    expect(out).toContain('scope = drive');
  });

  it('writes an empty token line by default', () => {
    expect(renderRcloneConf(CFG)).toContain('token = \n');
  });

  it('embeds a supplied token', () => {
    expect(renderRcloneConf(CFG, '{"access_token":"x"}')).toContain(
      'token = {"access_token":"x"}',
    );
  });
});

// ---------------------------------------------------------------------------
// renderDockerCompose
// ---------------------------------------------------------------------------
const SQLITE_CFG: ResolvedConfig = { ...CFG, persistence: 'sqlite' };
const SQLITE_DESKTOP_CFG: ResolvedConfig = { ...DESKTOP_CFG, persistence: 'sqlite' };

describe('renderDockerCompose (sqlite)', () => {
  it('rclone+sqlite: no postgres service, local db volume, sqlite DSN in env', () => {
    const c = renderDockerCompose(SQLITE_CFG);
    expect(c).not.toContain('pgvector/pgvector');
    expect(c).not.toContain('hivemind-postgres');
    expect(c).toContain('hivemind-dbdata:/data');
    expect(c).toContain('hivemind-gdrive');
    expect(renderEnvFile(SQLITE_CFG)).toContain(
      'DATABASE_URL=sqlite+aiosqlite:////data/hivemind.db',
    );
  });
  it('gdrive_desktop+sqlite: app-only, no postgres/gdrive service, binds the drive folder', () => {
    const c = renderDockerCompose(SQLITE_DESKTOP_CFG);
    expect(c).not.toContain('pgvector/pgvector');
    expect(c).not.toContain('hivemind-gdrive');
    expect(c).toContain('hivemind-dbdata:/data');
    expect(c).toContain(SQLITE_DESKTOP_CFG.gdriveDesktopPath as string);
  });
});

describe('renderDockerCompose', () => {
  const compose = renderDockerCompose(CFG);

  it('builds the app image locally and tags it hivemind:<version> (no registry pull)', () => {
    expect(compose).toContain('build:');
    expect(compose).toContain('context: ./app');
    expect(compose).toContain(`image: "${APP_IMAGE_NAME}:2.3.4-real"`);
    expect(compose).not.toContain('ghcr.io');
    expect(compose).toContain('INSTALL_EMBEDDINGS: "1"'); // CFG.embeddings === true
  });

  it('declares all three services', () => {
    expect(compose).toContain('gdrive:');
    expect(compose).toContain('postgres:');
    expect(compose).toContain('app:');
  });

  it('binds the host mount path into the container path for gdrive and app', () => {
    const occurrences = compose.split(`source: ${composeScalar(CFG.mountPath)}`).length - 1;
    expect(occurrences).toBe(2);
    expect(compose).toContain(`target: ${CONTAINER_HIVEMIND_PATH}`);
  });

  it('uses the pgvector image with a local data volume', () => {
    expect(compose).toContain('image: pgvector/pgvector:pg16');
    expect(compose).toContain('hivemind-pgdata:/var/lib/postgresql/data');
    expect(compose).toContain('driver: local');
  });

  it('publishes the MCP port via compose interpolation', () => {
    expect(compose).toContain('"127.0.0.1:${MCP_PORT}:${MCP_PORT}"');
  });

  it('references the sibling .env via env_file', () => {
    expect(compose).toContain('env_file:');
    expect(compose).toContain('./.env');
  });

  it('gates app startup on healthy gdrive + postgres', () => {
    expect(compose).toContain('condition: service_healthy');
    expect(compose).toContain('restart: true');
    expect(compose).toContain('HIVEMIND_FOLDER_ID: "FID123"');
  });

  it('quotes the rclone remote in the command (YAML would parse "hivemind:" as a map)', () => {
    expect(compose).toContain('- "hivemind:"');
    expect(compose).not.toContain('- hivemind:\n');
  });
});

// ---------------------------------------------------------------------------
// renderDockerCompose — gdrive_desktop backend
// ---------------------------------------------------------------------------
describe('renderDockerCompose (gdrive_desktop)', () => {
  const compose = renderDockerCompose(DESKTOP_CFG);

  it('has NO gdrive/rclone service', () => {
    expect(compose).not.toContain('gdrive:');
    expect(compose).not.toContain('rclone/rclone');
    expect(compose).not.toContain('SYS_ADMIN');
  });

  it('still runs postgres and app', () => {
    expect(compose).toContain('postgres:');
    expect(compose).toContain('app:');
    expect(compose).toContain('image: pgvector/pgvector:pg16');
  });

  it('bind-mounts the Drive-for-Desktop folder into the container path', () => {
    expect(compose).toContain(`source: ${composeScalar(DESKTOP_CFG.gdriveDesktopPath!)}`);
    expect(compose).toContain(`target: ${CONTAINER_HIVEMIND_PATH}`);
  });

  it('gates app only on postgres (no gdrive dependency)', () => {
    expect(compose).toContain('postgres:\n        condition: service_healthy');
    expect(compose).not.toContain('gdrive:\n        condition: service_healthy');
  });

  it('builds the app image locally (no registry) and publishes the MCP port', () => {
    expect(compose).toContain('build:');
    expect(compose).toContain('context: ./app');
    expect(compose).toContain(`image: "${APP_IMAGE_NAME}:2.3.4-real"`);
    expect(compose).not.toContain('ghcr.io');
    expect(compose).toContain('"127.0.0.1:${MCP_PORT}:${MCP_PORT}"');
  });
});

// ---------------------------------------------------------------------------
// embeddings toggle -> INSTALL_EMBEDDINGS build arg + EMBEDDING_MODEL
// ---------------------------------------------------------------------------
describe('embeddings toggle', () => {
  it('embeddings=true bakes the real model (INSTALL_EMBEDDINGS=1, real EMBEDDING_MODEL)', () => {
    const compose = renderDockerCompose({ ...CFG, embeddings: true });
    const env = renderEnvFile({ ...CFG, embeddings: true });
    expect(compose).toContain('INSTALL_EMBEDDINGS: "1"');
    expect(env).toContain(`EMBEDDING_MODEL=${EMBEDDING_MODEL}`);
  });

  it('embeddings=false is torch-free (INSTALL_EMBEDDINGS=0, EMBEDDING_MODEL=fake)', () => {
    const compose = renderDockerCompose({ ...CFG, embeddings: false });
    const env = renderEnvFile({ ...CFG, embeddings: false });
    expect(compose).toContain('INSTALL_EMBEDDINGS: "0"');
    expect(env).toContain(`EMBEDDING_MODEL=${FAKE_EMBEDDING_MODEL}`);
  });
});

// ---------------------------------------------------------------------------
// writeGeneratedFiles — side-effecting wrapper (hermetic temp dir)
// ---------------------------------------------------------------------------
describe('writeGeneratedFiles', () => {
  it('writes .env, rclone.conf and docker-compose.yml into ~/.hivemind', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const hmDir = path.join(dir, 'hivemind');
    const restore = withEnv({ HIVEMIND_DIR: hmDir });
    cleanups.push(restore);

    const paths = writeGeneratedFiles(CFG);
    expect(paths.env).toBe(path.join(hmDir, '.env'));
    expect(paths.compose).toBe(path.join(hmDir, 'docker-compose.yml'));
    expect(paths.rclone).toBe(path.join(hmDir, 'rclone.conf'));

    expect(readFileSync(paths.env, 'utf8')).toContain('OWNER_HANDLE=alice');
    expect(readFileSync(paths.compose, 'utf8')).toContain('services:');
    expect(readFileSync(paths.rclone!, 'utf8')).toContain('root_folder_id = FID123');
  });

  it('restricts rclone.conf permissions to 0600 (holds the secret)', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const restore = withEnv({ HIVEMIND_DIR: path.join(dir, 'hivemind') });
    cleanups.push(restore);

    const paths = writeGeneratedFiles(CFG);
    const mode = statSync(paths.rclone!).mode & 0o777;
    expect(mode).toBe(0o600);
  });

  it('embeds a supplied token into rclone.conf', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const restore = withEnv({ HIVEMIND_DIR: path.join(dir, 'hivemind') });
    cleanups.push(restore);

    const paths = writeGeneratedFiles(CFG, { token: 'TOKENVAL' });
    expect(readFileSync(paths.rclone!, 'utf8')).toContain('token = TOKENVAL');
  });

  it('reconciles rclone.conf when skipRclone is set', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const hmDir = path.join(dir, 'hivemind');
    const restore = withEnv({ HIVEMIND_DIR: hmDir });
    cleanups.push(restore);

    const paths = writeGeneratedFiles(CFG, { skipRclone: true });
    expect(paths.rclone).toBe(path.join(hmDir, 'rclone.conf'));
    expect(existsSync(path.join(hmDir, 'rclone.conf'))).toBe(true);
    expect(existsSync(paths.env)).toBe(true);
    expect(existsSync(paths.compose)).toBe(true);
  });

  it('never writes rclone.conf for the gdrive_desktop backend', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const hmDir = path.join(dir, 'hivemind');
    const restore = withEnv({ HIVEMIND_DIR: hmDir });
    cleanups.push(restore);

    const paths = writeGeneratedFiles(DESKTOP_CFG);
    expect(paths.rclone).toBeUndefined();
    expect(existsSync(path.join(hmDir, 'rclone.conf'))).toBe(false);
    expect(readFileSync(paths.compose, 'utf8')).not.toContain('gdrive:');
  });
});

// ---------------------------------------------------------------------------
// stageAppSource — copies the bundled app into ~/.hivemind/app (self-sufficient)
// ---------------------------------------------------------------------------
describe('stageAppSource', () => {
  it('copies the bundled app source into ~/.hivemind/app (clean + recopy)', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const hmDir = path.join(dir, 'hivemind');
    const restore = withEnv({ HIVEMIND_DIR: hmDir });
    cleanups.push(restore);

    // Fake bundled source.
    const src = path.join(dir, 'bundle');
    mkdirSync(path.join(src, 'hivemind_app'), { recursive: true });
    writeFileSync(path.join(src, 'Dockerfile'), 'FROM python:3.11-slim\n');
    writeFileSync(path.join(src, 'hivemind_app', '__init__.py'), '');

    // Pre-existing stale content must be removed.
    mkdirSync(path.join(hmDir, 'app'), { recursive: true });
    writeFileSync(path.join(hmDir, 'app', 'STALE'), 'old');

    const dest = stageAppSource(src);
    expect(dest).toBe(path.join(hmDir, 'app'));
    expect(existsSync(path.join(dest, 'Dockerfile'))).toBe(true);
    expect(existsSync(path.join(dest, 'hivemind_app', '__init__.py'))).toBe(true);
    expect(existsSync(path.join(dest, 'STALE'))).toBe(false); // cleaned
  });

  it('throws a clear error when the bundle is missing (package not built)', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const restore = withEnv({ HIVEMIND_DIR: path.join(dir, 'hivemind') });
    cleanups.push(restore);
    expect(() => stageAppSource(path.join(dir, 'does-not-exist'))).toThrow(/not found|not built/);
  });
});


describe('deployment hardening', () => {
  it.each([CFG, DESKTOP_CFG, SQLITE_CFG, SQLITE_DESKTOP_CFG])('keeps all variants local', (cfg) => {
    const compose = renderDockerCompose(cfg);
    expect(compose).toContain('pull_policy: never');
    expect(compose).toContain('"127.0.0.1:${MCP_PORT}:${MCP_PORT}"');
    expect(compose).toContain('create_host_path: false');
    expect(appImageTag({ ...cfg, embeddings: false })).not.toBe(appImageTag({ ...cfg, embeddings: true }));
  });

  it.each(['/tmp/a # comment', '/tmp/a: b', '/tmp/$HOME/${USER}', '/tmp/quote"and\\slash', '/tmp/a\nb'])('escapes literal YAML/Compose paths: %s', (mountPath) => {
    const rendered = composeScalar(mountPath);
    expect(JSON.parse(rendered).replace(/\$\$/g, '$')).toBe(mountPath);
    expect(renderDockerCompose({ ...CFG, mountPath })).toContain(`source: ${rendered}`);
  });

  it('preserves credentials, custom options and other remotes when the hive changes', () => {
    const original = '[other]\ntype = local\n[hivemind]\ntype = drive\nroot_folder_id = OLD\ntoken = SECRET\nclient_id = CUSTOM\nteam_drive = TEAM\n[next]\ntype = local\n';
    const updated = reconcileRcloneConf(CFG, original);
    expect(updated).toContain('root_folder_id = FID123');
    expect(updated).not.toContain('OLD');
    expect(updated).toContain('token = SECRET');
    expect(updated).toContain('client_id = CUSTOM');
    expect(updated).toContain('team_drive = TEAM');
    expect(updated).toContain('[next]\ntype = local');
    expect(reconcileRcloneConf(CFG, updated)).toBe(updated);
  });

  it('changes data identity when the remote changes under the same mount', () => {
    expect(hiveIdentity(CFG)).not.toBe(hiveIdentity({ ...CFG, folderId: 'OTHER' }));
  });
});
