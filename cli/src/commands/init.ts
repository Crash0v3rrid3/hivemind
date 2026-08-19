/**
 * `hivemind init` — scaffold ~/.hivemind.conf from the shipped example template.
 */
import { copyFileSync, existsSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import type { Command } from 'commander';
import { hivemindConfPath, templatesDir } from '../paths.js';

export function register(program: Command): void {
  program
    .command('init')
    .description('Scaffold ~/.hivemind.conf from the example template')
    .option('-f, --force', 'overwrite an existing ~/.hivemind.conf', false)
    .action((opts: { force?: boolean }) => {
      const dest = hivemindConfPath();
      if (existsSync(dest) && !opts.force) {
        console.log(`~/.hivemind.conf already exists at ${dest} (use --force to overwrite).`);
        return;
      }
      const src = path.join(templatesDir(), 'hivemind.conf.example');
      if (existsSync(src)) {
        copyFileSync(src, dest);
      } else {
        // Fallback: write a minimal valid skeleton if the template is missing.
        writeFileSync(
          dest,
          [
            'gdrive_folder_url = "https://drive.google.com/drive/folders/REPLACE_ME"',
            'username = "your-handle"',
            'mcp_port = 7777',
            '',
          ].join('\n'),
          'utf8',
        );
      }
      console.log(`Wrote ${dest}`);
      console.log('Edit it (gdrive_folder_url, username), then run: hivemind setup');
      // Surface the current contents so the user knows what to edit.
      console.log('\n--- current contents ---');
      console.log(readFileSync(dest, 'utf8').trimEnd());
    });
}
