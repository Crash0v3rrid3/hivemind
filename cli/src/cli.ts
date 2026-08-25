#!/usr/bin/env node
/**
 * @browserstack/hivemind — host CLI entry point.
 *
 * Provisions and operates a local, serverless, append-only shared team memory
 * exposed over MCP. Every command group is registered from src/commands/*.
 */
import { Command } from 'commander';
import { cliVersion } from './config.js';
import { register as registerInit } from './commands/init.js';
import { register as registerConfig } from './commands/config.js';
import { register as registerSetup } from './commands/setup.js';
import { register as registerLifecycle } from './commands/lifecycle.js';
import { register as registerDoctor } from './commands/doctor.js';
import { register as registerInstall } from './commands/install.js';
import { register as registerUninstall } from './commands/uninstall.js';
import { register as registerAdmin } from './commands/admin.js';

function buildProgram(): Command {
  const program = new Command();
  program
    .name('hivemind')
    .description('Hivemind — serverless, append-only shared team memory over MCP')
    .version(cliVersion(), '-v, --version', 'print the CLI version');

  registerInit(program);
  registerConfig(program);
  registerSetup(program);
  registerLifecycle(program);
  registerDoctor(program);
  registerInstall(program);
  registerUninstall(program);
  registerAdmin(program);

  program.showHelpAfterError();
  return program;
}

async function main(): Promise<void> {
  const program = buildProgram();
  await program.parseAsync(process.argv);
}

main().catch((err: unknown) => {
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(1);
});
