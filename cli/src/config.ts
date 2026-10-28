/**
 * Configuration loading, validation, and resolution for the Hivemind CLI.
 *
 * `~/.hivemind.conf` (TOML) is the human source of truth. The effective config
 * is resolved with precedence:  built-in defaults < conf file < environment.
 *
 * Every transformation here is a PURE, exported function (string/object in ->
 * value out, throwing on invalid input) so the whole module is unit-testable
 * without touching the real home directory. `loadResolvedConfig()` is the only
 * function that reads the filesystem, and it delegates all logic to the pure
 * helpers.
 */
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse as parseToml } from 'smol-toml';
import { hivemindConfPath } from './paths.js';

/** Raw shape parsed from the TOML file (all keys optional, snake_case). */
export interface RawConf {
  gdrive_folder_url?: string;
  username?: string;
  mcp_port?: number;
  roster?: string[];
  mount_path?: string;
  export_interval_sec?: number;
  import_interval_sec?: number;
  image_tag?: string;
  mount_backend?: string;
  gdrive_desktop_path?: string;
  embeddings?: boolean;
  deploy_backend?: string;
  persistence?: string;
}

/** How the hive folder is mounted into the app container. */
export const MOUNT_BACKENDS = ['rclone', 'gdrive_desktop'] as const;
export type MountBackend = (typeof MOUNT_BACKENDS)[number];

/** Which runtime operates the stack: nix (process-compose, no Docker) or docker. */
export const DEPLOY_BACKENDS = ['nix', 'docker'] as const;
export type DeployBackend = (typeof DEPLOY_BACKENDS)[number];

/** Persistence engine: postgres+pgvector (a DB server) or embedded sqlite+sqlite-vec. */
export const PERSISTENCE_BACKENDS = ['postgres', 'sqlite'] as const;
export type Persistence = (typeof PERSISTENCE_BACKENDS)[number];

/** Fully-resolved, validated config used by every command. */
export interface ResolvedConfig {
  /** Which mount backend to generate for: rclone FUSE, or Google Drive for Desktop. */
  mountBackend: MountBackend;
  /** Drive folder URL — required for rclone, optional for gdrive_desktop; null when unset. */
  gdriveFolderUrl: string | null;
  /** Drive folder id (from the URL) — null when no URL is configured (gdrive_desktop). */
  folderId: string | null;
  /**
   * Absolute host path to the `hivemind/` folder inside the Google Drive for
   * Desktop mount. Set only when mountBackend === 'gdrive_desktop', else null.
   */
  gdriveDesktopPath: string | null;
  /** OWNER_HANDLE — the authoritative author for all local writes. */
  ownerHandle: string;
  mcpPort: number;
  /** Explicit roster; empty means "use the admin roster.json in the hive". */
  roster: string[];
  mountPath: string;
  exportIntervalSec: number;
  importIntervalSec: number;
  imageTag: string;
  /**
   * Whether the app image bakes the real embedder (sentence-transformers/torch).
   * true (default) -> INSTALL_EMBEDDINGS=1 + the real BAAI model (self-sufficient
   * semantic search). false -> a torch-free image + EMBEDDING_MODEL=fake, for
   * lightweight/offline/testing use.
   */
  embeddings: boolean;
  /**
   * Runtime backend: 'nix' (default; process-compose, no Docker) or 'docker'. When
   * 'nix' is selected but the nix binary is unavailable, commands fall back to docker
   * at runtime (resolved by effectiveBackend in nix.ts, not here).
   */
  deployBackend: DeployBackend;
  /** Persistence: 'postgres' (default, pgvector server) or 'sqlite' (embedded sqlite-vec, no DB server). */
  persistence: Persistence;
}

/** Built-in defaults (lowest precedence). */
export interface ConfigDefaults {
  mcpPort: number;
  mountPath: string;
  exportIntervalSec: number;
  importIntervalSec: number;
  imageTag: string;
}

const HANDLE_RE = /^[a-z0-9][a-z0-9._-]*$/;
const FOLDERS_RE = /\/folders\/([A-Za-z0-9_-]+)/;
const ID_QUERY_RE = /[?&]id=([A-Za-z0-9_-]+)/;

/** Error raised for any invalid or missing configuration value. */
export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ConfigError';
  }
}

/** Read this CLI package's version (used as the default app image tag). */
export function cliVersion(): string {
  try {
    const here = path.dirname(fileURLToPath(import.meta.url));
    const pkgPath = path.join(here, '..', 'package.json');
    const pkg = JSON.parse(readFileSync(pkgPath, 'utf8')) as { version?: string };
    return pkg.version ?? '0.0.0';
  } catch {
    return '0.0.0';
  }
}

/** Built-in defaults with the image tag pinned to the CLI version. */
export function defaultConfig(): ConfigDefaults {
  return {
    mcpPort: 7777,
    mountPath: '/mnt/hivemind',
    exportIntervalSec: 60,
    importIntervalSec: 60,
    imageTag: cliVersion(),
  };
}

/**
 * Extract the Google Drive folder id from a share URL.
 * Accepts both `/folders/<id>` and `?id=<id>` / `&id=<id>` forms.
 * Throws {@link ConfigError} on any URL that contains neither (junk rejection).
 */
export function extractFolderId(url: string): string {
  if (typeof url !== 'string' || url.trim() === '') {
    throw new ConfigError('gdrive_folder_url is empty');
  }
  const folders = FOLDERS_RE.exec(url);
  if (folders && folders[1]) return folders[1];
  const query = ID_QUERY_RE.exec(url);
  if (query && query[1]) return query[1];
  throw new ConfigError(
    `could not extract a Drive folder id from "${url}" ` +
      '(expected a /folders/<id> or ?id=<id> URL)',
  );
}

/** Resolve + validate the mount backend (defaults to 'rclone'). */
export function resolveMountBackend(value: string | undefined): MountBackend {
  const v = value ?? 'rclone';
  if ((MOUNT_BACKENDS as readonly string[]).includes(v)) return v as MountBackend;
  throw new ConfigError(
    `invalid mount_backend "${v}" (must be one of ${MOUNT_BACKENDS.join(', ')})`,
  );
}

/** Resolve + validate the persistence engine (defaults to 'postgres'). */
export function resolvePersistence(value: string | undefined): Persistence {
  const v = value ?? 'postgres';
  if ((PERSISTENCE_BACKENDS as readonly string[]).includes(v)) return v as Persistence;
  throw new ConfigError(
    `invalid persistence "${v}" (must be one of ${PERSISTENCE_BACKENDS.join(', ')})`,
  );
}

/** Resolve + validate the deploy backend (defaults to 'nix'). */
export function resolveDeployBackend(value: string | undefined): DeployBackend {
  const v = value ?? 'nix';
  if ((DEPLOY_BACKENDS as readonly string[]).includes(v)) return v as DeployBackend;
  throw new ConfigError(
    `invalid deploy_backend "${v}" (must be one of ${DEPLOY_BACKENDS.join(', ')})`,
  );
}

/** Resolve the embeddings toggle (defaults to true). Env value is a string. */
export function resolveEmbeddings(
  envVal: string | undefined,
  rawVal: boolean | undefined,
): boolean {
  if (envVal !== undefined) {
    const v = envVal.trim().toLowerCase();
    if (['1', 'true', 'yes', 'on'].includes(v)) return true;
    if (['0', 'false', 'no', 'off', ''].includes(v)) return false;
    throw new ConfigError(`invalid HIVEMIND_EMBEDDINGS "${envVal}" (use true or false)`);
  }
  if (typeof rawVal === 'boolean') return rawVal;
  if (rawVal !== undefined) throw new ConfigError('embeddings must be a boolean');
  return true; // default: real embedder (self-sufficient)
}

/** Validate a member handle against `^[a-z0-9][a-z0-9._-]*$`. Returns it on success. */
export function validateHandle(handle: string): string {
  if (typeof handle !== 'string' || !HANDLE_RE.test(handle)) {
    throw new ConfigError(`invalid handle "${handle}" (must match ${HANDLE_RE.source})`);
  }
  return handle;
}

/** Coerce + validate a port into the 1..65535 range. Returns the integer port. */
export function validatePort(port: number | string): number {
  const n = typeof port === 'string' ? Number(port) : port;
  if (!Number.isInteger(n) || n < 1 || n > 65535) {
    throw new ConfigError(`invalid port "${port}" (must be an integer in 1..65535)`);
  }
  return n;
}

/** Parse a positive-integer interval; throws on non-positive or non-integer. */
function validateInterval(value: number | string, key: string): number {
  const n = typeof value === 'string' ? Number(value) : value;
  if (!Number.isInteger(n) || n < 1) {
    throw new ConfigError(`invalid ${key} "${value}" (must be a positive integer)`);
  }
  return n;
}

/** Parse TOML config text into a {@link RawConf}. Pure. */
export function parseConf(tomlText: string): RawConf {
  const parsed = parseToml(tomlText) as Record<string, unknown>;
  validateRawConf(parsed);
  return parsed as RawConf;
}

/** Reject malformed TOML values before any coercion or defaults hide them. */
function validateRawConf(raw: Record<string, unknown>): void {
  const types: Record<string, string> = {
    gdrive_folder_url: 'string',
    username: 'string',
    mcp_port: 'number',
    mount_path: 'string',
    export_interval_sec: 'number',
    import_interval_sec: 'number',
    image_tag: 'string',
    mount_backend: 'string',
    gdrive_desktop_path: 'string',
    embeddings: 'boolean',
    deploy_backend: 'string',
    persistence: 'string',
  };
  for (const [key, value] of Object.entries(raw)) {
    if (key === 'roster') {
      if (!Array.isArray(value) || value.some((v) => typeof v !== 'string')) {
        throw new ConfigError('roster must be an array of handles');
      }
      value.forEach((v: string) => validateHandle(v));
    } else if (!(key in types) || typeof value !== types[key]) {
      throw new ConfigError(
        `invalid ${key}: expected ${types[key] ?? 'a known configuration key'}`,
      );
    }
  }
}

/** Split a comma-separated roster env value into trimmed, non-empty handles. */
function splitRoster(value: string): string[] {
  return value
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

/**
 * Resolve raw conf + environment into a validated {@link ResolvedConfig}.
 * PURE: pass the env explicitly. Precedence is defaults < conf < env.
 * Throws {@link ConfigError} when a required value is missing or any value is invalid.
 */
export function resolveConfig(
  raw: RawConf,
  env: NodeJS.ProcessEnv = {},
  defaults: ConfigDefaults = defaultConfig(),
): ResolvedConfig {
  validateRawConf(raw as Record<string, unknown>);
  // mount backend (defaults to rclone)
  const mountBackend = resolveMountBackend(env.HIVEMIND_MOUNT_BACKEND ?? raw.mount_backend);

  // Drive folder URL / id: required for rclone, optional for gdrive_desktop.
  const gdriveFolderUrl: string | null =
    env.HIVEMIND_GDRIVE_FOLDER_URL ?? raw.gdrive_folder_url ?? null;
  let folderId: string | null = null;
  if (mountBackend === 'rclone') {
    if (!gdriveFolderUrl) {
      throw new ConfigError(
        'gdrive_folder_url is required for mount_backend="rclone" (set it in ~/.hivemind.conf)',
      );
    }
    folderId = extractFolderId(gdriveFolderUrl);
  } else if (gdriveFolderUrl) {
    // Optional under gdrive_desktop, but validate it if the user supplied one.
    folderId = extractFolderId(gdriveFolderUrl);
  }

  // gdrive_desktop_path: required (and absolute) for the gdrive_desktop backend.
  let gdriveDesktopPath: string | null = null;
  if (mountBackend === 'gdrive_desktop') {
    const rawDesktop = env.HIVEMIND_GDRIVE_DESKTOP_PATH ?? raw.gdrive_desktop_path;
    if (!rawDesktop) {
      throw new ConfigError(
        'gdrive_desktop_path is required for mount_backend="gdrive_desktop" ' +
          '(the hivemind/ folder inside your Google Drive for Desktop mount)',
      );
    }
    if (!path.isAbsolute(rawDesktop)) {
      throw new ConfigError(`gdrive_desktop_path must be an absolute path (got "${rawDesktop}")`);
    }
    gdriveDesktopPath = rawDesktop;
  }

  // username -> OWNER_HANDLE (required)
  const rawHandle = env.HIVEMIND_USERNAME ?? raw.username;
  if (!rawHandle) {
    throw new ConfigError('username is required (set it in ~/.hivemind.conf)');
  }
  const ownerHandle = validateHandle(rawHandle);

  // mcp_port
  const rawPort = env.HIVEMIND_MCP_PORT ?? raw.mcp_port ?? defaults.mcpPort;
  const mcpPort = validatePort(rawPort);

  // roster (optional)
  let roster: string[];
  if (env.HIVEMIND_ROSTER !== undefined) {
    roster = splitRoster(env.HIVEMIND_ROSTER);
  } else if (Array.isArray(raw.roster)) {
    roster = raw.roster.map(validateHandle);
  } else {
    roster = [];
  }

  roster.forEach(validateHandle);

  // mount_path
  const mountPath = env.HIVEMIND_MOUNT_PATH ?? raw.mount_path ?? defaults.mountPath;

  if (!path.isAbsolute(mountPath)) throw new ConfigError('mount_path must be an absolute path');

  // intervals
  const exportIntervalSec = validateInterval(
    env.HIVEMIND_EXPORT_INTERVAL_SEC ?? raw.export_interval_sec ?? defaults.exportIntervalSec,
    'export_interval_sec',
  );
  const importIntervalSec = validateInterval(
    env.HIVEMIND_IMPORT_INTERVAL_SEC ?? raw.import_interval_sec ?? defaults.importIntervalSec,
    'import_interval_sec',
  );

  // image_tag
  const imageTag = env.HIVEMIND_IMAGE_TAG ?? raw.image_tag ?? defaults.imageTag;

  // embeddings toggle (default true)
  const embeddings = resolveEmbeddings(env.HIVEMIND_EMBEDDINGS, raw.embeddings);

  // deploy backend (default nix; runtime falls back to docker when nix is absent)
  const deployBackend = resolveDeployBackend(env.HIVEMIND_DEPLOY_BACKEND ?? raw.deploy_backend);

  // persistence engine (default postgres)
  const persistence = resolvePersistence(env.HIVEMIND_PERSISTENCE ?? raw.persistence);

  return {
    mountBackend,
    gdriveFolderUrl,
    folderId,
    gdriveDesktopPath,
    ownerHandle,
    mcpPort,
    roster,
    mountPath,
    exportIntervalSec,
    importIntervalSec,
    imageTag,
    embeddings,
    deployBackend,
    persistence,
  };
}

/**
 * The host path that actually holds the hive's per-member folders for this
 * backend: the Google Drive for Desktop folder for `gdrive_desktop`, otherwise
 * the (FUSE) mount path.
 */
export function hostHivePath(cfg: ResolvedConfig): string {
  return cfg.mountBackend === 'gdrive_desktop' && cfg.gdriveDesktopPath
    ? cfg.gdriveDesktopPath
    : cfg.mountPath;
}

/** Read + parse the conf file, returning an empty conf when it does not exist. */
export function loadConf(confPath: string = hivemindConfPath()): RawConf {
  if (!existsSync(confPath)) return {};
  const text = readFileSync(confPath, 'utf8');
  return parseConf(text);
}

/**
 * Load the conf file (if present) and resolve it against the live environment.
 * The one filesystem-touching entry point; all logic lives in the pure helpers.
 */
export function loadResolvedConfig(confPath: string = hivemindConfPath()): ResolvedConfig {
  return resolveConfig(loadConf(confPath), process.env);
}
