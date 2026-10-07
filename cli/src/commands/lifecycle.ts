/**
 * `hivemind up|down|status|logs` — lifecycle against ~/.hivemind, dispatched on the
 * effective backend: Nix (process-compose, default on macOS) or Docker Compose.
 * `up` first regenerates/stages so the running stack reflects ~/.hivemind.conf.
 */
import { existsSync } from 'node:fs';
import type { Command } from 'commander';
import { ConfigError, loadResolvedConfig, type ResolvedConfig } from '../config.js';
import { composeDown, composeLogs, composeStatus, composeUp, prePullPostgres } from '../docker.js';
import { effectiveBackend, nixDown, nixLogs, nixStatus, nixUp, waitHealthy } from '../nix.js';
import { stageAppSource, writeGeneratedFiles } from '../generate.js';
import { hivemindConfPath } from '../paths.js';

function requireConfigured(): boolean {
  if (!existsSync(hivemindConfPath())) {
    console.error('~/.hivemind.conf not found — run: hivemind init');
    return false;
  }
  return true;
}

/** Resolve the effective backend for down/status/logs; default to docker if unreadable. */
function resolveBackend(): 'nix' | 'docker' {
  try {
    return effectiveBackend(loadResolvedConfig()).backend;
  } catch {
    return 'docker';
  }
}

/** Nix backend: stage the app flake, start detached, wait for health. */
async function runUpNix(cfg: ResolvedConfig): Promise<number> {
  if (cfg.mountBackend !== 'gdrive_desktop') {
    console.error(
      'deploy_backend="nix" requires mount_backend="gdrive_desktop" ' +
        '(set it in ~/.hivemind.conf, or use deploy_backend="docker").',
    );
    return 1;
  }
  try {
    stageAppSource();
  } catch (err) {
    console.error((err as Error).message);
    return 1;
  }
  console.log('Starting Hivemind stack via Nix (first run fetches the closure)...');
  const code = nixUp(cfg);
  if (code !== 0) return code;
  const ok = await waitHealthy(cfg.mcpPort);
  if (!ok) {
    console.error('stack started but /healthz did not become ready in time — check `hivemind logs`.');
    return 1;
  }
  console.log(`Hivemind is up — MCP at http://127.0.0.1:${cfg.mcpPort}/mcp`);
  return 0;
}

/** Docker backend: regenerate derived files, stage source, build + start detached. */
function runUpDocker(cfg: ResolvedConfig): number {
  try {
    writeGeneratedFiles(cfg, { skipRclone: true });
    stageAppSource();
  } catch (err) {
    console.error((err as Error).message);
    return 1;
  }
  console.log('Starting Hivemind stack (first run builds the image locally, ~1 min)...');
  prePullPostgres();
  return composeUp();
}

/** Regenerate/stage then start the stack on the effective backend. Returns an exit code. */
export async function runUp(): Promise<number> {
  if (!requireConfigured()) return 1;
  let cfg: ResolvedConfig;
  try {
    cfg = loadResolvedConfig();
  } catch (err) {
    if (err instanceof ConfigError) {
      console.error(`config error: ${err.message}`);
      return 1;
    }
    console.error((err as Error).message);
    return 1;
  }
  const { backend, note } = effectiveBackend(cfg);
  if (note) console.warn(note);
  return backend === 'nix' ? runUpNix(cfg) : runUpDocker(cfg);
}

export function register(program: Command): void {
  program
    .command('up')
    .description('Regenerate derived files from the conf, then start the stack (detached)')
    .action(async () => {
      process.exitCode = await runUp();
    });

  program
    .command('down')
    .description('Stop the Hivemind stack')
    .action(() => {
      process.exitCode = resolveBackend() === 'nix' ? nixDown() : composeDown();
    });

  program
    .command('status')
    .description('Show the status of the Hivemind services')
    .action(() => {
      process.exitCode = resolveBackend() === 'nix' ? nixStatus() : composeStatus();
    });

  program
    .command('logs')
    .description('Show logs from the Hivemind services')
    .option('-f, --follow', 'follow log output', false)
    .argument('[service]', 'limit to a single service (postgres|app)')
    .action((service: string | undefined, opts: { follow?: boolean }) => {
      if (resolveBackend() === 'nix') {
        const args = [service ?? 'app'];
        if (opts.follow) args.push('-f');
        process.exitCode = nixLogs(args);
      } else {
        process.exitCode = composeLogs({ follow: opts.follow, service });
      }
    });
}
