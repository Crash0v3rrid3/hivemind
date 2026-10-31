import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { checkedProcess } from './checked-process.mjs';
import {
  cpSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
  existsSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { createServer } from 'node:http';
const root = dirname(dirname(fileURLToPath(import.meta.url)));
const require = createRequire(import.meta.url);
const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
const temp = mkdtempSync(join(tmpdir(), 'hivemind-package-'));
const home = join(temp, 'home');
mkdirSync(home);
// No inherited credentials, NODE_PATH or HIVEMIND overrides.
const env = {
  PATH: process.env.PATH,
  HOME: home,
  USERPROFILE: home,
  HIVEMIND_TEST_HOME: home,
  npm_config_cache: join(temp, 'cache'),
  npm_config_userconfig: join(temp, 'npmrc'),
  npm_config_offline: 'true',
  npm_config_ignore_scripts: 'true',
};
writeFileSync(env.npm_config_userconfig, '');
let server;
try {
  const packed = JSON.parse(
    execFileSync(
      process.platform === 'win32' ? 'npm.cmd' : 'npm',
      ['pack', '--json', '--ignore-scripts', '--pack-destination', temp],
      { cwd: root, env, encoding: 'utf8', timeout: 60000 },
    ),
  )[0];
  const names = packed.files.map((f) => f.path);
  for (const file of [
    'dist/cli.js',
    'dist/templates/docker-compose.yml',
    'templates/hivemind.conf.example',
    'app/Dockerfile',
    'app/flake.nix',
    'app/flake.lock',
    'app/pyproject.toml',
    'app/hivemind_app/__init__.py',
    'app/hivemind_app/retrieval_fixture.json',
    'LICENSE',
  ])
    assert(names.includes(file), file);
  assert(
    !names.some((name) =>
      /(^|\/)(tests?|node_modules|__pycache__|\.venv|\.git|\.env|rclone\.conf|\.npmrc)(\/|$)|\.pyc$/.test(
        name,
      ),
    ),
  );
  const consumer = join(temp, 'consumer');
  mkdirSync(consumer);
  execFileSync('tar', ['-xzf', join(temp, packed.filename), '-C', consumer], { timeout: 10000 });
  const installed = join(consumer, 'package');
  // Copy actual installed production dependencies, dereferencing checkout symlinks.
  for (const name of Object.keys(pkg.dependencies)) {
    let dep = dirname(require.resolve(name));
    while (
      !existsSync(join(dep, 'package.json')) ||
      JSON.parse(readFileSync(join(dep, 'package.json'), 'utf8')).name !== name
    )
      dep = dirname(dep);
    cpSync(dep, join(installed, 'node_modules', name), { recursive: true, dereference: true });
  }
  const shipped = JSON.parse(readFileSync(join(installed, 'package.json'), 'utf8'));
  assert.equal(shipped.version, pkg.version);
  for (const file of ['app/pyproject.toml', 'app/flake.nix'])
    assert(readFileSync(join(installed, file), 'utf8').includes(`version = "${pkg.version}"`));
  const blockedBin = join(temp, 'blocked-bin');
  const externalCalls = join(temp, 'unexpected-external-calls');
  mkdirSync(blockedBin);
  for (const command of ['docker', 'docker-compose', 'nix', 'rclone', 'process-compose']) {
    writeFileSync(
      join(blockedBin, command),
      `#!/bin/sh
printf '%s\\n' '${command}' >> '${externalCalls}'
exit 99
`,
      { mode: 0o755 },
    );
  }
  const run = async (args, expected = 0, extra = {}) => {
    const result = await checkedProcess(
      process.execPath,
      [join(installed, shipped.bin.hivemind), ...args],
      {
        cwd: consumer,
        env: { ...env, ...extra, PATH: blockedBin },
        timeout: 10000,
        maxBuffer: 1048576,
      },
      expected,
    );
    assert(!existsSync(externalCalls), 'CLI unexpectedly attempted external service commands');
    return result;
  };
  assert.equal((await run(['--version'])).stdout.trim(), pkg.version);
  for (const command of [
    'init',
    'config',
    'setup',
    'up',
    'down',
    'status',
    'logs',
    'doctor',
    'install',
    'uninstall',
    'install-usage-instructions',
    'install-mcp-server',
    'install-enforcement',
    'uninstall-usage-instructions',
    'uninstall-mcp-server',
    'uninstall-enforcement',
    'guard',
    'admin',
    'sync',
  ])
    assert.match((await run([command, '--help'])).stdout, /Usage:/);
  await run(['bogus-command'], 1);
  await run(['sync', 'status', '--bogus'], 1);
  await run(['guard'], 0, { HIVEMIND_GUARD_DISABLE: '1' });
  await run(['init']);
  const conf = join(home, '.hivemind.conf');
  assert.equal(
    readFileSync(conf, 'utf8'),
    readFileSync(join(installed, 'dist/templates/hivemind.conf.example'), 'utf8'),
  );
  const scaffold = readFileSync(conf, 'utf8');
  await run(['init']);
  assert.equal(readFileSync(conf, 'utf8'), scaffold);
  writeFileSync(conf, 'username=true');
  await run(['config', '--print'], 1);
  await run(['sync', 'status'], 1);
  await run(['up'], 1);
  // A controlled editor boundary must propagate its exit code.
  await run(['config', '--edit'], 7, { EDITOR: 'exit 7' });
  await run(['guard'], 2, { HIVEMIND_MCP_URL: 'not a URL' });
  let body;
  let mode = 'valid';
  const requests = [];
  server = createServer((req, res) => {
    requests.push([req.method, req.url]);
    if (mode === 'redirect') {
      res.writeHead(302, { Location: '/secret' });
      res.end();
    } else if (mode === 'http') {
      res.writeHead(503);
      res.end('PRIVATE');
    } else if (mode === 'truncated') {
      res.writeHead(200, { 'Content-Length': 10000 });
      res.end('{');
    } else if (mode === 'schema')
      res.end(JSON.stringify({ ...body, remote_delivery: 'confirmed' }));
    else if (mode === 'malformed') res.end('PRIVATE not JSON');
    else if (mode === 'oversized') res.end('x'.repeat(65537));
    else res.end(JSON.stringify(body));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  writeFileSync(
    conf,
    `username="alice"\ngdrive_folder_url="https://drive.google.com/drive/folders/ABC"\nmcp_port=${port}\n`,
  );
  const before = readFileSync(conf, 'utf8');
  const cycle = {
    last_attempt_at: null,
    last_success_at: null,
    last_failure_category: null,
    problem_count: 0,
    problems: [],
  };
  body = {
    service: 'hivemind',
    health: 'ok',
    telemetry_started_at: '2026-10-28T12:00:00Z',
    loops_enabled: true,
    remote_delivery: 'unknown',
    pending_local_exports: 0,
    quarantined_ids: 0,
    import: cycle,
    export: cycle,
    recovery_guidance: [],
  };
  assert.deepEqual(JSON.parse((await run(['sync', 'status', '--json'])).stdout), body);
  body = { ...body, health: 'degraded', pending_local_exports: 7 };
  assert.deepEqual(JSON.parse((await run(['sync', 'status', '--json'], 1)).stdout), body);
  assert.match((await run(['sync', 'status'], 1)).stdout, /Remote delivery: unknown/);
  body = JSON.parse(readFileSync(join(root, 'test/fixtures/sync-path-status.json'), 'utf8'));
  assert.deepEqual(JSON.parse((await run(['sync', 'status', '--json'], 1)).stdout), body);
  for (mode of ['schema', 'malformed', 'oversized', 'truncated', 'redirect', 'http']) {
    const result = await run(['sync', 'status'], 1);
    assert.match(result.stderr, /Cannot read valid Hivemind sync status/);
    assert(!result.stderr.includes('PRIVATE'));
    assert.equal(result.stdout, '');
  }
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
  server = undefined;
  await run(['sync', 'status'], 1);
  assert(requests.every(([method, url]) => method === 'GET' && url === '/sync/status'));
  assert.equal(readFileSync(conf, 'utf8'), before);
  assert.deepEqual(readdirSync(home), ['.hivemind.conf']);
  console.log(
    `Package smoke passed: ${packed.filename}; isolated consumer and sync transport failures.`,
  );
} finally {
  if (server) {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
  rmSync(temp, { recursive: true, force: true });
}
