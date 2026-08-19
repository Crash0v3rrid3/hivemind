/**
 * `hivemind up|down|status|logs` — Docker Compose lifecycle against ~/.hivemind.
 * `up` first regenerates the derived files from the current conf so the running
 * stack always reflects ~/.hivemind.conf.
 */
import { existsSync } from 'node:fs';
import type { Command } from 'commander';
import { ConfigError, loadResolvedConfig } from '../config.js';
import { composeDown, composeLogs, composeStatus, composeUp } from '../docker.js';
import { writeGeneratedFiles } from '../generate.js';
import { hivemindConfPath } from '../paths.js';

function requireConfigured(): boolean {
  if (!existsSync(hivemindConfPath())) {
    console.error('~/.hivemind.conf not found — run: hivemind init');
    return false;
  }
  return true;
}

/** Regenerate derived files then `docker compose up -d`. Returns an exit code. */
export function runUp(): number {
  if (!requireConfigured()) return 1;
  try {
    const cfg = loadResolvedConfig();
    writeGeneratedFiles(cfg, { skipRclone: true });
  } catch (err) {
    if (err instanceof ConfigError) {
      console.error(`config error: ${err.message}`);
      return 1;
    }
    throw err;
  }
  console.log('Starting Hivemind stack (first boot ~60-90s while the model loads)...');
  return composeUp();
}

export function register(program: Command): void {
  program
    .command('up')
    .description('Regenerate derived files from the conf, then start the stack (detached)')
    .action(() => {
      process.exitCode = runUp();
    });

  program
    .command('down')
    .description('Stop the Hivemind stack')
    .action(() => {
      process.exitCode = composeDown();
    });

  program
    .command('status')
    .description('Show the status of the Hivemind services')
    .action(() => {
      process.exitCode = composeStatus();
    });

  program
    .command('logs')
    .description('Show logs from the Hivemind services')
    .option('-f, --follow', 'follow log output', false)
    .argument('[service]', 'limit to a single service (gdrive|postgres|app)')
    .action((service: string | undefined, opts: { follow?: boolean }) => {
      process.exitCode = composeLogs({ follow: opts.follow, service });
    });
}
