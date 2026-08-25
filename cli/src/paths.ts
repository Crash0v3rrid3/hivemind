/**
 * Filesystem path resolution for Hivemind's durable host state.
 *
 * Every path is computed through a small set of functions so tests can redirect
 * them at the real home directory via environment variables. Nothing here touches
 * the filesystem; these are pure lookups over `process.env` + `os.homedir()`.
 *
 * Override variables (used by the test-suite and power users):
 *   HIVEMIND_TEST_HOME   overrides the base home directory for ALL derived paths
 *   HIVEMIND_DIR         overrides ~/.hivemind directly
 *   HIVEMIND_CONF        overrides ~/.hivemind.conf directly
 *   HIVEMIND_CLAUDE_DIR  overrides ~/.claude directly
 *   HIVEMIND_CLAUDE_JSON overrides ~/.claude.json directly
 */
import { existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/** Base home directory. Overridable with HIVEMIND_TEST_HOME for hermetic tests. */
export function homeDir(): string {
  return process.env.HIVEMIND_TEST_HOME ?? os.homedir();
}

/** The ~/.hivemind directory holding generated files (.env, rclone.conf, compose, guard.sh). */
export function hivemindDir(): string {
  return process.env.HIVEMIND_DIR ?? path.join(homeDir(), '.hivemind');
}

/** The ~/.hivemind.conf TOML file — the human source of truth for the CLI. */
export function hivemindConfPath(): string {
  return process.env.HIVEMIND_CONF ?? path.join(homeDir(), '.hivemind.conf');
}

/** The ~/.claude directory (Claude Code global config). */
export function claudeDir(): string {
  return process.env.HIVEMIND_CLAUDE_DIR ?? path.join(homeDir(), '.claude');
}

/** The ~/.claude/CLAUDE.md global instructions file. */
export function claudeMdPath(): string {
  return path.join(claudeDir(), 'CLAUDE.md');
}

/** The ~/.claude/settings.json file (hooks live here). */
export function claudeSettingsPath(): string {
  return path.join(claudeDir(), 'settings.json');
}

/** The ~/.claude.json file (MCP server registrations live here). */
export function claudeJsonPath(): string {
  return process.env.HIVEMIND_CLAUDE_JSON ?? path.join(homeDir(), '.claude.json');
}

/** Generated container env file. */
export function envPath(): string {
  return path.join(hivemindDir(), '.env');
}

/** Generated rclone config (holds the Drive OAuth token — the only secret on disk). */
export function rcloneConfPath(): string {
  return path.join(hivemindDir(), 'rclone.conf');
}

/** Generated docker-compose.yml. */
export function composePath(): string {
  return path.join(hivemindDir(), 'docker-compose.yml');
}

/** Self-contained guard script installed for the Claude UserPromptSubmit hook. */
export function guardScriptPath(): string {
  return path.join(hivemindDir(), 'guard.sh');
}

/**
 * The Python app source BUNDLED inside the published package (`<pkgRoot>/app`).
 * Created at build time by `scripts/bundle-app.mjs` and shipped via package.json
 * `files`. This is what makes the package self-sufficient: `hivemind up` builds
 * the app image locally from here — nothing is pulled from a registry.
 */
export function bundledAppDir(): string {
  const here = path.dirname(fileURLToPath(import.meta.url));
  // dist/paths.js -> <pkgRoot>/app ; src/paths.ts (tests/dev) -> cli/app
  return path.join(here, '..', 'app');
}

/** Where the app source is staged for the running stack (`~/.hivemind/app`). */
export function hivemindAppDir(): string {
  return path.join(hivemindDir(), 'app');
}

/**
 * Locate the bundled `templates/` directory. When running from the compiled
 * package this sits at dist/templates (copied at build time); in a source
 * checkout it is at the package root. Both are probed.
 */
export function templatesDir(): string {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const candidates = [
    path.join(here, 'templates'), // dist/templates (packaged)
    path.join(here, '..', 'templates'), // package-root templates (dev)
  ];
  for (const c of candidates) {
    if (existsSync(c)) return c;
  }
  // Fall back to the packaged location even if it does not yet exist so callers
  // get a stable, informative path in error messages.
  return candidates[0]!;
}
