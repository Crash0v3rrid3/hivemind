import { describe, it, expect, afterEach } from 'vitest';
import { readFileSync, statSync, existsSync } from 'node:fs';
import path from 'node:path';
import {
  APP_IMAGE_REPO,
  CONTAINER_HIVEMIND_PATH,
  DATABASE_URL,
  EMBEDDING_DIM,
  EMBEDDING_MODEL,
  renderDockerCompose,
  renderEnvFile,
  renderRcloneConf,
  writeGeneratedFiles,
} from '../src/generate.js';
import type { ResolvedConfig } from '../src/config.js';
import { makeTempDir, withEnv } from './helpers.js';

const cleanups: Array<() => void> = [];
afterEach(() => {
  while (cleanups.length) cleanups.pop()!();
});

const CFG: ResolvedConfig = {
  gdriveFolderUrl: 'https://drive.google.com/drive/folders/FID123',
  folderId: 'FID123',
  ownerHandle: 'alice',
  mcpPort: 8123,
  roster: ['alice', 'bob', 'carol'],
  mountPath: '/home/alice/hive-mnt',
  exportIntervalSec: 30,
  importIntervalSec: 45,
  imageTag: '2.3.4',
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
describe('renderDockerCompose', () => {
  const compose = renderDockerCompose(CFG);

  it('pins the app image to the resolved tag', () => {
    expect(compose).toContain(`image: ${APP_IMAGE_REPO}:2.3.4`);
  });

  it('declares all three services', () => {
    expect(compose).toContain('gdrive:');
    expect(compose).toContain('postgres:');
    expect(compose).toContain('app:');
  });

  it('binds the host mount path into the container path for gdrive and app', () => {
    const occurrences = compose.split(`source: ${CFG.mountPath}`).length - 1;
    expect(occurrences).toBe(2);
    expect(compose).toContain(`target: ${CONTAINER_HIVEMIND_PATH}`);
  });

  it('uses the pgvector image with a local data volume', () => {
    expect(compose).toContain('image: pgvector/pgvector:pg16');
    expect(compose).toContain('hivemind-pgdata:/var/lib/postgresql/data');
    expect(compose).toContain('driver: local');
  });

  it('publishes the MCP port via compose interpolation', () => {
    expect(compose).toContain('"${MCP_PORT}:${MCP_PORT}"');
  });

  it('references the sibling .env via env_file', () => {
    expect(compose).toContain('env_file:');
    expect(compose).toContain('./.env');
  });

  it('gates app startup on healthy gdrive + postgres', () => {
    expect(compose).toContain('condition: service_healthy');
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

  it('skips rclone.conf when skipRclone is set', () => {
    const { dir, cleanup } = makeTempDir();
    cleanups.push(cleanup);
    const hmDir = path.join(dir, 'hivemind');
    const restore = withEnv({ HIVEMIND_DIR: hmDir });
    cleanups.push(restore);

    const paths = writeGeneratedFiles(CFG, { skipRclone: true });
    expect(paths.rclone).toBeUndefined();
    expect(existsSync(path.join(hmDir, 'rclone.conf'))).toBe(false);
    expect(existsSync(paths.env)).toBe(true);
    expect(existsSync(paths.compose)).toBe(true);
  });
});
