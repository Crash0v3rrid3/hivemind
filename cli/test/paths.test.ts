import { describe, it, expect, afterEach } from 'vitest';
import path from 'node:path';
import {
  claudeDir,
  claudeJsonPath,
  claudeMdPath,
  claudeSettingsPath,
  composePath,
  envPath,
  guardScriptPath,
  hivemindConfPath,
  hivemindDir,
  homeDir,
  rcloneConfPath,
} from '../src/paths.js';
import { withEnv } from './helpers.js';

const cleanups: Array<() => void> = [];
afterEach(() => {
  while (cleanups.length) cleanups.pop()!();
});

describe('paths — HIVEMIND_TEST_HOME redirection', () => {
  it('derives every path from the overridden home', () => {
    const restore = withEnv({
      HIVEMIND_TEST_HOME: '/tmp/fakehome',
      HIVEMIND_DIR: undefined,
      HIVEMIND_CONF: undefined,
      HIVEMIND_CLAUDE_DIR: undefined,
      HIVEMIND_CLAUDE_JSON: undefined,
    });
    cleanups.push(restore);

    expect(homeDir()).toBe('/tmp/fakehome');
    expect(hivemindDir()).toBe('/tmp/fakehome/.hivemind');
    expect(hivemindConfPath()).toBe('/tmp/fakehome/.hivemind.conf');
    expect(claudeDir()).toBe('/tmp/fakehome/.claude');
    expect(claudeMdPath()).toBe('/tmp/fakehome/.claude/CLAUDE.md');
    expect(claudeSettingsPath()).toBe('/tmp/fakehome/.claude/settings.json');
    expect(claudeJsonPath()).toBe('/tmp/fakehome/.claude.json');
    expect(envPath()).toBe('/tmp/fakehome/.hivemind/.env');
    expect(rcloneConfPath()).toBe('/tmp/fakehome/.hivemind/rclone.conf');
    expect(composePath()).toBe('/tmp/fakehome/.hivemind/docker-compose.yml');
    expect(guardScriptPath()).toBe('/tmp/fakehome/.hivemind/guard.sh');
  });

  it('honours the direct per-path overrides over the home base', () => {
    const restore = withEnv({
      HIVEMIND_TEST_HOME: '/tmp/fakehome',
      HIVEMIND_DIR: '/custom/hive',
      HIVEMIND_CONF: '/custom/hive.conf',
      HIVEMIND_CLAUDE_DIR: '/custom/claude',
      HIVEMIND_CLAUDE_JSON: '/custom/claude.json',
    });
    cleanups.push(restore);

    expect(hivemindDir()).toBe('/custom/hive');
    expect(hivemindConfPath()).toBe('/custom/hive.conf');
    expect(claudeDir()).toBe('/custom/claude');
    expect(claudeJsonPath()).toBe('/custom/claude.json');
    // derived-from-dir paths follow the overridden dirs
    expect(envPath()).toBe(path.join('/custom/hive', '.env'));
    expect(claudeMdPath()).toBe(path.join('/custom/claude', 'CLAUDE.md'));
  });
});
