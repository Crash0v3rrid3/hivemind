/**
 * Installer commands that wire Hivemind into Claude Code, plus the `guard` probe
 * and the one-shot `install` orchestrator.
 *
 *   install-usage-instructions  marker block into ~/.claude/CLAUDE.md
 *   install-mcp-server          http entry into ~/.claude.json
 *   install-enforcement         guard.sh + UserPromptSubmit hook into settings.json
 *   guard                       HTTP probe of the MCP url (exit non-zero if down)
 *   install                     setup -> up -> doctor -> the three install-* steps
 */
import type { Command } from 'commander';
import { ConfigError, loadResolvedConfig, type ResolvedConfig } from '../config.js';
import { installEnforcementHook, installMcpServer, installUsageInstructions } from '../claude.js';
import { installGuardScript, mcpUrlForPort, runGuard, DEFAULT_MCP_URL } from '../guard.js';
import { guardScriptPath } from '../paths.js';
import { runSetup } from './setup.js';
import { runUp } from './lifecycle.js';
import { runDoctor } from './doctor.js';

/** Resolve config if possible, else null (some installers work without a valid conf). */
function tryLoadConfig(): ResolvedConfig | null {
  try {
    return loadResolvedConfig();
  } catch (err) {
    if (err instanceof ConfigError) return null;
    throw err;
  }
}

function doInstallUsage(): void {
  const p = installUsageInstructions();
  console.log(`Installed usage instructions into ${p}`);
}

function doInstallMcp(): void {
  const cfg = tryLoadConfig();
  const port = cfg?.mcpPort ?? 7777;
  const p = installMcpServer(port);
  console.log(`Registered MCP server (port ${port}) in ${p}`);
}

function doInstallEnforcement(): void {
  const cfg = tryLoadConfig();
  const defaultUrl = cfg ? mcpUrlForPort(cfg.mcpPort) : DEFAULT_MCP_URL;
  const scriptPath = installGuardScript(defaultUrl);
  const settingsPath = installEnforcementHook(scriptPath);
  console.log(`Wrote guard script ${scriptPath}`);
  console.log(`Merged UserPromptSubmit hook into ${settingsPath}`);
}

export function register(program: Command): void {
  program
    .command('install-usage-instructions')
    .description('Insert/refresh the Hivemind usage block in ~/.claude/CLAUDE.md')
    .action(() => {
      doInstallUsage();
    });

  program
    .command('install-mcp-server')
    .description('Upsert the Hivemind http MCP server into ~/.claude.json')
    .action(() => {
      doInstallMcp();
    });

  program
    .command('install-enforcement')
    .description('Install guard.sh and merge the UserPromptSubmit hook into settings.json')
    .action(() => {
      doInstallEnforcement();
    });

  program
    .command('guard')
    .description('Probe the Hivemind MCP endpoint; exit non-zero if unreachable')
    .action(async () => {
      // Default the probe URL to the configured mcp_port (matching the installed
      // guard.sh), falling back to the built-in default when no valid conf exists.
      const cfg = tryLoadConfig();
      const fallbackUrl = cfg ? mcpUrlForPort(cfg.mcpPort) : DEFAULT_MCP_URL;
      process.exitCode = await runGuard(process.env, fallbackUrl);
    });

  program
    .command('install')
    .description('One-shot: setup -> up -> doctor -> install usage/mcp/enforcement')
    .action(async () => {
      const setupCode = runSetup();
      if (setupCode !== 0) {
        console.error('install: setup failed, aborting.');
        process.exitCode = setupCode;
        return;
      }
      const upCode = runUp();
      if (upCode !== 0) {
        console.error('install: `up` failed, aborting.');
        process.exitCode = upCode;
        return;
      }
      console.log('First `up` compiled the app image locally (one-time); later runs reuse the cache.');
      const doctorCode = await runDoctor();
      if (doctorCode !== 0) {
        console.warn(
          'install: doctor reported issues (the stack may still be booting) — ' +
            'continuing with Claude integration; re-run `hivemind doctor` shortly.',
        );
      }
      doInstallUsage();
      doInstallMcp();
      doInstallEnforcement();

      console.log(`\nHivemind installed. Guard script: ${guardScriptPath()}`);
      console.log('Claude will now require Hivemind before every prompt.');
    });
}
