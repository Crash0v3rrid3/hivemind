import { expect, it } from 'vitest';
import { checkedProcess } from '../scripts/checked-process.mjs';

it('accepts only expected normal child exits', async () => {
  await checkedProcess(process.execPath, ['-e', 'process.exit(0)'], { timeout: 1000 });
  await checkedProcess(process.execPath, ['-e', 'process.exit(7)'], { timeout: 1000 }, 7);
  await expect(
    checkedProcess(process.execPath, ['-e', 'process.exit(1)'], { timeout: 1000 }),
  ).rejects.toThrow();
});
it('cannot mistake a timed-out child for successful completion', async () => {
  await expect(
    checkedProcess(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { timeout: 100 }),
  ).rejects.toThrow('did not exit normally');
});
it('cannot mistake a signalled child for successful completion', async () => {
  await expect(
    checkedProcess(process.execPath, ['-e', "process.kill(process.pid, 'SIGTERM')"], {
      timeout: 1000,
    }),
  ).rejects.toThrow('did not exit normally');
});
it('rejects spawn failures even when nonzero exit is expected', async () => {
  await expect(
    checkedProcess('/nonexistent/hivemind-executable', [], { timeout: 1000 }, 1),
  ).rejects.toThrow('did not exit normally');
});
