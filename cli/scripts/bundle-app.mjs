// Bundles the Python app source into the CLI package (cli/app) at build time so
// the published package is SELF-SUFFICIENT: `hivemind up` builds the app image
// locally from this bundled source and never pulls from a registry.
//
// Whitelist-copies only what the Docker build needs (Dockerfile, pyproject.toml,
// README.md, hivemind_app/**) — tests, venvs, caches, and compose files are
// excluded by simply not copying them. Idempotent: the dest is cleaned first.
import { cp, mkdir, rm } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const cliRoot = join(here, '..');
const repoRoot = join(cliRoot, '..');
const appSrc = join(repoRoot, 'app');
const dest = join(cliRoot, 'app');

const INCLUDE = ['Dockerfile', 'pyproject.toml', 'README.md', 'hivemind_app'];

if (!existsSync(appSrc)) {
  console.error(`bundle-app: source app dir not found at ${appSrc}`);
  process.exit(1);
}

await rm(dest, { recursive: true, force: true });
await mkdir(dest, { recursive: true });
for (const entry of INCLUDE) {
  const from = join(appSrc, entry);
  if (!existsSync(from)) {
    console.error(`bundle-app: required entry missing: ${from}`);
    process.exit(1);
  }
  await cp(from, join(dest, entry), {
    recursive: true,
    // Defensive: never copy caches/venvs even if nested under hivemind_app.
    filter: (src) =>
      !/(^|\/)(__pycache__|\.venv|\.mypy_cache|\.pytest_cache|\.ruff_cache)(\/|$)/.test(src) &&
      !src.endsWith('.pyc'),
  });
}
console.log(`Bundled app source -> ${dest} (${INCLUDE.join(', ')})`);
