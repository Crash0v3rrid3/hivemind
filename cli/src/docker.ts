/**
 * Docker Compose lifecycle helpers, always operating on the generated stack in
 * ~/.hivemind (docker-compose.yml + .env). Thin wrappers over `docker compose`
 * via child_process; each returns the child's exit code.
 */
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { composePath, envPath, hivemindDir } from './paths.js';

/** Build the base `docker compose` argv (project file + env file). PURE. */
export function composeBaseArgs(): string[] {
  return ['compose', '-f', composePath(), '--env-file', envPath()];
}

/** True if the generated compose file exists. */
export function composeConfigExists(): boolean {
  return existsSync(composePath());
}

/**
 * Run `docker compose <args>` against the Hivemind stack, inheriting stdio so the
 * user sees live output. Returns the exit code (127 if docker is missing).
 */
export function runCompose(args: string[]): number {
  const result = spawnSync('docker', [...composeBaseArgs(), ...args], {
    stdio: 'inherit',
    cwd: hivemindDir(),
  });
  if (result.error) {
    const err = result.error as NodeJS.ErrnoException;
    if (err.code === 'ENOENT') {
      process.stderr.write('docker not found on PATH — install Docker to run Hivemind.\n');
      return 127;
    }
    process.stderr.write(`failed to run docker compose: ${err.message}\n`);
    return 1;
  }
  return result.status ?? 1;
}

/** Run `docker compose <args>` capturing stdout as a string (for status/doctor probes). */
export function runComposeCapture(args: string[]): { code: number; stdout: string; stderr: string } {
  const result = spawnSync('docker', [...composeBaseArgs(), ...args], {
    encoding: 'utf8',
    cwd: hivemindDir(),
  });
  if (result.error) {
    return { code: 127, stdout: '', stderr: (result.error as Error).message };
  }
  return { code: result.status ?? 1, stdout: result.stdout ?? '', stderr: result.stderr ?? '' };
}

/** `docker compose up -d` (detached). */
export function composeUp(): number {
  return runCompose(['up', '-d']);
}

/** `docker compose down`. */
export function composeDown(): number {
  return runCompose(['down']);
}

/** `docker compose ps` (status table). */
export function composeStatus(): number {
  return runCompose(['ps']);
}

/** `docker compose logs`, optionally following and/or scoped to a service. */
export function composeLogs(opts: { follow?: boolean; service?: string } = {}): number {
  const args = ['logs'];
  if (opts.follow) args.push('-f');
  if (opts.service) args.push(opts.service);
  return runCompose(args);
}
