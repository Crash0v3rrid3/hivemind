/**
 * `hivemind config [--edit|--print]` — validate and show the resolved config, or
 * open the conf file in $EDITOR.
 */
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import type { Command } from 'commander';
import { ConfigError, loadResolvedConfig } from '../config.js';
import { hivemindConfPath } from '../paths.js';

function printResolved(): void {
  const cfg = loadResolvedConfig();
  const view = {
    ownerHandle: cfg.ownerHandle,
    gdriveFolderUrl: cfg.gdriveFolderUrl,
    folderId: cfg.folderId,
    mcpPort: cfg.mcpPort,
    roster: cfg.roster.length > 0 ? cfg.roster : '(admin roster.json in hive)',
    mountPath: cfg.mountPath,
    exportIntervalSec: cfg.exportIntervalSec,
    importIntervalSec: cfg.importIntervalSec,
    imageTag: cfg.imageTag,
  };
  console.log(JSON.stringify(view, null, 2));
}

export function register(program: Command): void {
  program
    .command('config')
    .description('Validate and print the resolved config, or edit ~/.hivemind.conf')
    .option('--edit', 'open ~/.hivemind.conf in $EDITOR', false)
    .option('--print', 'print the resolved, validated config (default)', false)
    .action((opts: { edit?: boolean; print?: boolean }) => {
      const confPath = hivemindConfPath();
      if (opts.edit) {
        if (!existsSync(confPath)) {
          console.error(`${confPath} does not exist — run: hivemind init`);
          process.exitCode = 1;
          return;
        }
        const editor = process.env.EDITOR ?? process.env.VISUAL ?? 'vi';
        const res = spawnSync(editor, [confPath], { stdio: 'inherit' });
        process.exitCode = res.status ?? 0;
        return;
      }
      // Default action (and --print) validates + shows the resolved config.
      try {
        printResolved();
      } catch (err) {
        if (err instanceof ConfigError) {
          console.error(`config error: ${err.message}`);
          process.exitCode = 1;
          return;
        }
        throw err;
      }
    });
}
