/**
 * The Hivemind enforcement guard.
 *
 * A self-contained POSIX `guard.sh` is installed into ~/.hivemind and wired into
 * Claude Code's UserPromptSubmit hook. It probes the local MCP endpoint before every
 * prompt and blocks work when Hivemind is unreachable. The guard script MUST NOT call
 * npx (it runs on the hot path of every prompt); it uses curl and standard POSIX tools.
 *
 * The `hivemind guard` CLI command performs the same probe in-process so it can be
 * run manually and in `doctor`.
 */
import { chmodSync, mkdirSync, writeFileSync } from 'node:fs';
import { shellQuote } from './claude.js';
import { guardScriptPath, hivemindDir } from './paths.js';

/** Default MCP URL when nothing else is configured. */
export const DEFAULT_MCP_URL = 'http://localhost:7777/mcp';

/** Build the MCP url for a given localhost port. */
export function mcpUrlForPort(port: number): string {
  return `http://localhost:${port}/mcp`;
}

/**
 * Resolve the effective MCP url. Precedence: HIVEMIND_MCP_URL env > explicit
 * fallback (e.g. from resolved config) > the built-in default. PURE.
 */
export function resolveGuardUrl(
  env: NodeJS.ProcessEnv = {},
  fallback: string = DEFAULT_MCP_URL,
): string {
  return env.HIVEMIND_MCP_URL ?? fallback ?? DEFAULT_MCP_URL;
}

/**
 * Render the self-contained guard.sh. The baked-in default url is used only when
 * HIVEMIND_MCP_URL is unset at runtime. PURE.
 */
export function guardScriptContent(defaultUrl: string = DEFAULT_MCP_URL): string {
  return `#!/bin/sh
[ "$HIVEMIND_GUARD_DISABLE" = "1" ] && exit 0
DEFAULT_URL=${shellQuote(defaultUrl)}
URL="\${HIVEMIND_MCP_URL:-$DEFAULT_URL}"
URL="\${URL%%#*}"
URL="\${URL%%'?'*}"
URL="\${URL%/}"
URL="\${URL%/mcp}/healthz"
BODY=$(curl -fsS -m 2 -w '%{http_code}' "$URL") &&
  [ "\${BODY#\${BODY%???}}" = "200" ] &&
  printf '%s' "$BODY" | grep -Eq '"service"[[:space:]]*:[[:space:]]*"hivemind"' &&
  printf '%s' "$BODY" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' && exit 0
echo "Hivemind MCP unreachable — run: npx -y @crash0v3rrid3/hivemind up (first boot ~60-90s)" >&2
exit 2
`;
}

/** Write guard.sh into ~/.hivemind and make it executable. Returns its absolute path. */
export function installGuardScript(defaultUrl: string = DEFAULT_MCP_URL): string {
  mkdirSync(hivemindDir(), { recursive: true });
  const p = guardScriptPath();
  writeFileSync(p, guardScriptContent(defaultUrl), 'utf8');
  chmodSync(p, 0o755);
  return p;
}

/**
 * Probe the MCP endpoint over HTTP with a short timeout. Returns true if the server
 * reports Hivemind readiness, false on HTTP errors, wrong services or timeout.
 */
export async function probeMcp(url: string, timeoutMs = 2000): Promise<boolean> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const healthUrl = new URL(url);
    healthUrl.pathname = healthUrl.pathname.replace(/\/mcp\/?$/, '') + '/healthz';
    healthUrl.search = '';
    healthUrl.hash = '';
    const response = await fetch(healthUrl, {
      method: 'GET',
      signal: controller.signal,
      redirect: 'error',
    });
    if (response.status !== 200) return false;
    const body = (await response.json()) as { service?: unknown; status?: unknown };
    return body.service === 'hivemind' && body.status === 'ok';
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Run the guard logic in-process (used by the `guard` command). Honors
 * HIVEMIND_GUARD_DISABLE=1. Returns the process exit code (0 = ok/bypassed,
 * 2 = blocking failure) and writes the unreachable message to stderr on failure.
 */
export async function runGuard(
  env: NodeJS.ProcessEnv = process.env,
  fallbackUrl: string = DEFAULT_MCP_URL,
): Promise<number> {
  if (env.HIVEMIND_GUARD_DISABLE === '1') return 0;
  const url = resolveGuardUrl(env, fallbackUrl);
  const ok = await probeMcp(url);
  if (ok) return 0;
  process.stderr.write(
    'Hivemind MCP unreachable — run: npx -y @crash0v3rrid3/hivemind up (first boot ~60-90s)\n',
  );
  return 2;
}
