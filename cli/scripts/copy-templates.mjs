// Copies the templates/ directory contents into dist/ after tsc build so that
// packaged consumers can resolve template files relative to the compiled code.
// The published package ships both dist/ and templates/ (see package.json "files"),
// and at runtime template lookup resolves against the templates/ dir next to dist/.
import { cp, mkdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..');
const src = join(root, 'templates');
const dest = join(root, 'dist', 'templates');

await mkdir(dest, { recursive: true });
await cp(src, dest, { recursive: true });
console.log(`Copied templates -> ${dest}`);
