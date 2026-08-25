// Compile the CLI and produce the single, publish-ready artifact.
//
// Because the package is self-sufficient (the Python app source is bundled into
// cli/app at build time and the app image is built locally by `hivemind up`),
// "everything" is exactly the npm package contents. This script:
//   1. runs the full build (tsc -> dist, copy templates, bundle app, LICENSE),
//   2. runs `npm pack` to get the EXACT publishable tarball (respects the `files`
//      allowlist) — this .tgz is directly `npm publish`-able,
//   3. repackages that same file set into a single .zip for convenience.
//
// Outputs land in cli/dist-pkg/ (gitignored):
//   browserstack-hivemind-<version>.tgz   (canonical — `npm publish <tgz>`)
//   hivemind-<version>.zip                (single zip of the publishable package/)
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { existsSync, mkdirSync, readdirSync, rmSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const cliRoot = join(here, '..');
const require = createRequire(import.meta.url);
const { version } = require(join(cliRoot, 'package.json'));

const outDir = join(cliRoot, 'dist-pkg');
const stage = join(outDir, '_stage');

const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
const run = (cmd, args, cwd = cliRoot) => execFileSync(cmd, args, { cwd, stdio: 'inherit' });

// 1. Clean + build.
rmSync(outDir, { recursive: true, force: true });
mkdirSync(stage, { recursive: true });
run(npm, ['run', 'build']);

// 2. npm pack -> the exact publishable tarball.
run(npm, ['pack', '--pack-destination', outDir]);
const tgz = readdirSync(outDir).find((f) => f.endsWith('.tgz'));
if (!tgz) {
  console.error('package: npm pack produced no .tgz');
  process.exit(1);
}

// 3. Unpack that tarball's package/ dir and zip it into a single .zip.
run('tar', ['-xzf', join(outDir, tgz), '-C', stage]);
if (!existsSync(join(stage, 'package'))) {
  console.error('package: unexpected tarball layout (no package/ dir)');
  process.exit(1);
}
const zipName = `hivemind-${version}.zip`;
run('zip', ['-rq', join(outDir, zipName), 'package'], stage);
rmSync(stage, { recursive: true, force: true });

// 4. Report.
const kb = (p) => `${Math.round(statSync(p).size / 1024)} KB`;
console.log(`\nPublish artifacts in ${outDir}:`);
console.log(`  ${tgz}  (${kb(join(outDir, tgz))})  ->  npm publish "${join(outDir, tgz)}"`);
console.log(`  ${zipName}  (${kb(join(outDir, zipName))})  ->  single zip of the publishable package/`);
