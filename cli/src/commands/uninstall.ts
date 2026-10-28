/**
 * Uninstall commands — the inverses of `install` / `install-*`.
 *
 *   uninstall-usage-instructions  remove the block from ~/.claude/CLAUDE.md
 *   uninstall-mcp-server          remove the hivemind entry from ~/.claude.json
 *   uninstall-enforcement         remove the guard hook + delete guard.sh
 *   uninstall                     down the stack + the three above; --purge also
 *                                 drops the DB volume, ~/.hivemind, and the image
 *
 * NEVER touches the shared Drive folder / gdrive_desktop_path — that is the
 * team's memory. Uninstall removes only this local node's wiring and (with
 * --purge) its local data.
 */
import { spawnSync } from 'node:child_process';
import { existsSync, rmSync, unlinkSync } from 'node:fs';
import type { Command } from 'commander';
import { cliVersion } from '../config.js';
import {
  uninstallEnforcementHook,
  uninstallMcpServer,
  uninstallUsageInstructions,
} from '../claude.js';
import { composeConfigExists, composeDown } from '../docker.js';
import { nixDown, nixSocketPath, nixStateDir } from '../nix.js';
import { clearBackendRecord, recordedBackend } from './lifecycle.js';
import { guardScriptPath, hivemindDir } from '../paths.js';

function doUninstallUsage(): void {
  console.log(`Removed usage instructions from ${uninstallUsageInstructions()}`);
}

function doUninstallMcp(): void {
  console.log(`Removed MCP server entry from ${uninstallMcpServer()}`);
}

function doUninstallEnforcement(): void {
  const p = uninstallEnforcementHook(guardScriptPath());
  const script = guardScriptPath();
  if (existsSync(script)) unlinkSync(script);
  console.log(`Removed guard hook from ${p} and deleted ${script}`);
}

export function register(program: Command): void {
  program
    .command('uninstall-usage-instructions')
    .description('Remove the Hivemind usage block from ~/.claude/CLAUDE.md')
    .action(() => {
      doUninstallUsage();
    });

  program
    .command('uninstall-mcp-server')
    .description('Remove the Hivemind MCP server from ~/.claude.json')
    .action(() => {
      doUninstallMcp();
    });

  program
    .command('uninstall-enforcement')
    .description('Remove the UserPromptSubmit guard hook and delete guard.sh')
    .action(() => {
      doUninstallEnforcement();
    });

  program
    .command('uninstall')
    .description('Inverse of install: stop the stack + unwire Claude (--purge also removes data)')
    .option('--purge', 'also drop the Postgres volume, ~/.hivemind, and the local app image')
    .action((opts: { purge?: boolean }) => {
      const purge = Boolean(opts.purge);

      // Generated artifacts identify previously started backends even if the config
      // has since changed. Stop both when both have state before removing anything.
      let backend: 'nix' | 'docker' | undefined;
      try {
        backend = recordedBackend();
      } catch (err) {
        console.error(`uninstall: ${(err as Error).message}`);
        process.exitCode = 1;
        return;
      }
      if (backend === 'nix' || existsSync(nixSocketPath()) || existsSync(nixStateDir())) {
        if (nixDown() !== 0) {
          console.error('uninstall: Nix shutdown failed; local state retained.');
          process.exitCode = 1;
          return;
        }
      }
      if (backend === 'docker' || composeConfigExists()) {
        if (composeDown({ volumes: purge }) !== 0) {
          console.error('uninstall: Docker shutdown failed; local state retained.');
          process.exitCode = 1;
          return;
        }
      }
      clearBackendRecord();

      doUninstallEnforcement();
      doUninstallMcp();
      doUninstallUsage();

      if (purge) {
        const dir = hivemindDir();
        rmSync(dir, { recursive: true, force: true });
        console.log(`Purged ${dir}`);
        const image = `hivemind:${cliVersion()}`;
        spawnSync('docker', ['rmi', '-f', image], { stdio: 'ignore' });
        console.log(`Removed local image ${image} (if present)`);
      }

      console.log(
        '\nHivemind uninstalled from this node. Your shared Drive folder (team memory) was NOT touched.',
      );
      if (!purge) {
        console.log(
          'Re-run with --purge to also delete local data (~/.hivemind, DB volume, image).',
        );
      }
    });
}
