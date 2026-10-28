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
  console.log(JSON.stringify(cfg, null, 2));
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
        // $EDITOR is a shell command (e.g. code --wait); pass the path as a
        // positional argument so spaces and shell metacharacters stay literal.
        const res = spawnSync('/bin/sh', ['-c', `${editor} "$1"`, 'hivemind-editor', confPath], {
          stdio: 'inherit',
        });
        if (res.error) console.error(`could not start editor: ${res.error.message}`);
        if (res.signal) console.error(`editor terminated by ${res.signal}`);
        process.exitCode = res.status ?? 1;
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
