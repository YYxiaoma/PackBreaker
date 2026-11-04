import { describe, expect, it } from 'vitest';
import { waitForCookieCloudProfileSync } from './waitForCookieCloudProfileSync';

describe('CookieCloud 后台站点资料同步等待', () => {
  it('等待超过三分钟仍继续轮询，直到队列清空', async () => {
    let statusCalls = 0;
    let sleepCalls = 0;

    const result = await waitForCookieCloudProfileSync(
      async () => ({ pending_count: ++statusCalls <= 37 ? 1 : 0 }),
      () => true,
      {
        intervalMs: 5_000,
        sleep: async (milliseconds) => {
          expect(milliseconds).toBe(5_000);
          sleepCalls += 1;
        },
      },
    );

    expect(result).toBe('completed');
    expect(statusCalls).toBe(38);
    expect(sleepCalls).toBe(37);
  });

  it('组件离开后停止等待，不再发起下一次状态请求', async () => {
    let active = true;
    let statusCalls = 0;

    const result = await waitForCookieCloudProfileSync(
      async () => {
        statusCalls += 1;
        return { pending_count: 1 };
      },
      () => active,
      {
        sleep: async () => {
          active = false;
        },
      },
    );

    expect(result).toBe('stopped');
    expect(statusCalls).toBe(1);
  });

  it('状态接口不可用时返回明确结果，让调用方执行降级刷新', async () => {
    const result = await waitForCookieCloudProfileSync(
      async () => {
        throw new Error('temporary failure');
      },
      () => true,
      { sleep: async () => undefined },
    );

    expect(result).toBe('unavailable');
  });
});
