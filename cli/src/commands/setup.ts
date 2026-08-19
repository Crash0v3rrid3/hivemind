/**
 * `hivemind setup` — turn ~/.hivemind.conf into a ready-to-run stack:
 *   1. resolve + validate the config (extracts the Drive folder id)
 *   2. create the host mount path
 *   3. configure the rclone Drive remote (headless OAuth) scoped to the hive folder
 *   4. generate ~/.hivemind/{.env, rclone.conf, docker-compose.yml}
 *   5. best-effort ACL verification (full checks run in `doctor`)
 */
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import type { Command } from 'commander';
import { ConfigError, hostHivePath, loadResolvedConfig, type ResolvedConfig } from '../config.js';
import { writeGeneratedFiles } from '../generate.js';
import { rcloneConfPath } from '../paths.js';

function rcloneAvailable(): boolean {
  const res = spawnSync('rclone', ['version'], { stdio: 'ignore' });
  return !res.error && (res.status ?? 1) === 0;
}

/**
 * Configure the rclone `hivemind` remote scoped to the shared folder, writing into
 * ~/.hivemind/rclone.conf via RCLONE_CONFIG. Uses headless OAuth. Returns true if
 * rclone wrote the remote (so generate should not overwrite it).
 */
function configureRclone(cfg: ResolvedConfig): boolean {
  if (!rcloneAvailable()) {
    console.warn(
      'rclone not found on PATH — writing a placeholder rclone.conf.\n' +
        '  Install rclone and re-run `hivemind setup`, or complete the OAuth token manually.',
    );
    return false;
  }
  const args = [
    'config',
    'create',
    'hivemind',
    'drive',
    'scope=drive',
    `root_folder_id=${cfg.folderId}`,
  ];
  console.log('Configuring rclone Drive remote (a browser window may open for OAuth)...');
  const res = spawnSync('rclone', args, {
    stdio: 'inherit',
    env: { ...process.env, RCLONE_CONFIG: rcloneConfPath() },
  });
  if (res.error || (res.status ?? 1) !== 0) {
    console.warn('rclone config did not complete cleanly — writing a placeholder rclone.conf.');
    return false;
  }
  return true;
}

function ensureMountPath(cfg: ResolvedConfig): void {
  try {
    mkdirSync(cfg.mountPath, { recursive: true });
    console.log(`Ensured mount path ${cfg.mountPath}`);
  } catch (err) {
    console.warn(
      `Could not create mount path ${cfg.mountPath}: ${(err as Error).message}\n` +
        `  Create it manually (may require sudo): sudo mkdir -p ${cfg.mountPath} && ` +
        `sudo chown "$USER" ${cfg.mountPath}`,
    );
  }
}

function verifyAcls(cfg: ResolvedConfig): void {
  const ownFolder = path.join(hostHivePath(cfg), cfg.ownerHandle);
  if (existsSync(ownFolder)) {
    console.log(`Hive is active; own folder present at ${ownFolder}.`);
    console.log('Run `hivemind doctor` for full read/write ACL verification.');
  } else {
    console.log(
      'Hive not active yet — ACL verification will run once the stack is up ' +
        '(`hivemind up` then `hivemind doctor`).',
    );
  }
}

/**
 * Google Drive for Desktop backend: no rclone/OAuth. Verify the synced hive
 * folder exists and generate the derived files (no rclone.conf is written).
 */
function runSetupGdriveDesktop(cfg: ResolvedConfig): number {
  const hive = cfg.gdriveDesktopPath as string;
  console.log(`Google Drive for Desktop hive folder: ${hive}`);
  if (!existsSync(hive)) {
    console.warn(
      `Google Drive for Desktop folder not found at ${hive} — is Drive for Desktop ` +
        'running and the hivemind/ folder synced? Continuing; the app will pick it up ' +
        'once it appears.',
    );
  }
  const paths = writeGeneratedFiles(cfg); // rclone.conf skipped automatically
  console.log(`Wrote ${paths.env}`);
  console.log(`Wrote ${paths.compose}`);
  console.log('No rclone.conf needed (gdrive_desktop backend).');
  verifyAcls(cfg);
  console.log('\nSetup complete. Start the stack with: hivemind up');
  return 0;
}

/** Run the full setup flow. Returns a process exit code (0 = ok). */
export function runSetup(): number {
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

  console.log(`Owner handle: ${cfg.ownerHandle}`);
  console.log(`Mount backend: ${cfg.mountBackend}`);

  if (cfg.mountBackend === 'gdrive_desktop') {
    return runSetupGdriveDesktop(cfg);
  }

  console.log(`Drive folder id: ${cfg.folderId}`);
  ensureMountPath(cfg);
  const rcloneWritten = configureRclone(cfg);
  const paths = writeGeneratedFiles(cfg, { skipRclone: rcloneWritten });

  console.log(`Wrote ${paths.env}`);
  console.log(`Wrote ${paths.compose}`);
  if (paths.rclone) console.log(`Wrote ${paths.rclone}`);
  else console.log(`rclone remote configured at ${rcloneConfPath()}`);

  verifyAcls(cfg);
  console.log('\nSetup complete. Start the stack with: hivemind up');
  return 0;
}

export function register(program: Command): void {
  program
    .command('setup')
    .description('Configure rclone + generate ~/.hivemind derived files from the conf')
    .action(() => {
      process.exitCode = runSetup();
    });
}
