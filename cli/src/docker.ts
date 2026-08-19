/**
 * Docker Compose lifecycle helpers, always operating on the generated stack in
 * ~/.hivemind (docker-compose.yml + .env). Thin wrappers over Compose via
 * child_process; each returns the child's exit code.
 *
 * Works with either the Docker Compose v2 plugin (`docker compose`) or the
 * standalone `docker-compose` binary, preferring the plugin and falling back.
 */
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { composePath, envPath, hivemindDir } from './paths.js';

/** The resolved Compose launcher: a command plus its leading sub-args. */
export interface ComposeCmd {
  cmd: string;
  pre: string[];
}

let _resolved: ComposeCmd | null | undefined;

/**
 * Resolve how to invoke Compose on this host: prefer the `docker compose` v2
 * plugin, fall back to the standalone `docker-compose` binary. Returns null when
 * neither is available. Cached after the first probe.
 */
export function resolveComposeCmd(): ComposeCmd | null {
  if (_resolved !== undefined) return _resolved;
  const plugin = spawnSync('docker', ['compose', 'version'], { stdio: 'ignore' });
  if (!plugin.error && plugin.status === 0) {
    _resolved = { cmd: 'docker', pre: ['compose'] };
    return _resolved;
  }
  const standalone = spawnSync('docker-compose', ['version'], { stdio: 'ignore' });
  if (!standalone.error && standalone.status === 0) {
    _resolved = { cmd: 'docker-compose', pre: [] };
    return _resolved;
  }
  _resolved = null;
  return _resolved;
}

/** Reset the cached Compose resolution (tests only). */
export function _resetComposeCmd(): void {
  _resolved = undefined;
}

/** The project/env args shared by every Compose invocation (file + env file). PURE. */
export function composeBaseArgs(): string[] {
  return ['-f', composePath(), '--env-file', envPath()];
}

/** True if the generated compose file exists. */
export function composeConfigExists(): boolean {
  return existsSync(composePath());
}

function _composeMissing(): void {
  process.stderr.write(
    'Docker Compose not found — install Docker with the Compose plugin ' +
      '(`docker compose`) or the standalone `docker-compose` binary.\n',
  );
}

/**
 * Run Compose against the Hivemind stack, inheriting stdio so the user sees live
 * output. Returns the exit code (127 if Compose is unavailable).
 */
export function runCompose(args: string[]): number {
  const compose = resolveComposeCmd();
  if (!compose) {
    _composeMissing();
    return 127;
  }
  const result = spawnSync(compose.cmd, [...compose.pre, ...composeBaseArgs(), ...args], {
    stdio: 'inherit',
    cwd: hivemindDir(),
  });
  if (result.error) {
    const err = result.error as NodeJS.ErrnoException;
    if (err.code === 'ENOENT') {
      _composeMissing();
      return 127;
    }
    process.stderr.write(`failed to run Compose: ${err.message}\n`);
    return 1;
  }
  return result.status ?? 1;
}

/** Run Compose capturing stdout as a string (for status/doctor probes). */
export function runComposeCapture(args: string[]): { code: number; stdout: string; stderr: string } {
  const compose = resolveComposeCmd();
  if (!compose) {
    return { code: 127, stdout: '', stderr: 'Docker Compose not found' };
  }
  const result = spawnSync(compose.cmd, [...compose.pre, ...composeBaseArgs(), ...args], {
    encoding: 'utf8',
    cwd: hivemindDir(),
  });
  if (result.error) {
    return { code: 127, stdout: '', stderr: (result.error as Error).message };
  }
  return { code: result.status ?? 1, stdout: result.stdout ?? '', stderr: result.stderr ?? '' };
}

/** `<compose> up -d` (detached). */
export function composeUp(): number {
  return runCompose(['up', '-d']);
}

/** `<compose> down`. */
export function composeDown(): number {
  return runCompose(['down']);
}

/** `<compose> ps` (status table). */
export function composeStatus(): number {
  return runCompose(['ps']);
}

/** `<compose> logs`, optionally following and/or scoped to a service. */
export function composeLogs(opts: { follow?: boolean; service?: string } = {}): number {
  const args = ['logs'];
  if (opts.follow) args.push('-f');
  if (opts.service) args.push(opts.service);
  return runCompose(args);
}
