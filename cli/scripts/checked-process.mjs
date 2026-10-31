import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

// A rejected process can only satisfy an expected nonzero *normal* exit.
// Timeout, signal, spawn failure and output overflow must always fail the harness.
export async function checkedProcess(command, args, options, expected = 0) {
  let result;
  let completedNormally = false;
  try {
    result = await promisify(execFile)(command, args, options);
    completedNormally = true;
  } catch (error) {
    result = error;
    completedNormally = Number.isInteger(error.code) && !error.killed && !error.signal;
  }
  assert(completedNormally, `Child did not exit normally: ${result.message ?? args.join(' ')}`);
  assert.equal(result.code ?? 0, expected, `${args}: ${result.stderr}`);
  return result;
}
