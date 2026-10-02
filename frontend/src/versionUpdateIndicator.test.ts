import { describe, expect, it } from 'vitest';

import {
  BACKGROUND_RELEASE_CHECK_INTERVAL_MS,
  parsePendingUpgradeRequest,
  shouldReloadAfterUpgrade,
  shouldMarkUpdateUnseen,
} from './versionUpdateIndicator';

describe('版本更新提示', () => {
  it('后台版本检查间隔固定为 30 分钟', () => {
    expect(BACKGROUND_RELEASE_CHECK_INTERVAL_MS).toBe(30 * 60 * 1000);
  });

  it('只在弹窗关闭且发现新版本时标记为未读', () => {
    expect(shouldMarkUpdateUnseen(true, false)).toBe(true);
    expect(shouldMarkUpdateUnseen(true, true)).toBe(false);
    expect(shouldMarkUpdateUnseen(false, false)).toBe(false);
  });

  it('可以恢复刷新前保存的升级幂等请求', () => {
    const digest = 'sha256:' + 'a'.repeat(64);
    expect(
      parsePendingUpgradeRequest(
        JSON.stringify({
          idempotencyKey: 'pb-upgrade-123',
          targetVersion: '1.0.7',
          targetDigest: digest,
        }),
      ),
    ).toEqual({
      idempotencyKey: 'pb-upgrade-123',
      targetVersion: '1.0.7',
      targetDigest: digest,
    });
    expect(parsePendingUpgradeRequest('{broken')).toBeNull();
  });

  it('升级成功后按 helper request id 只触发一次自动刷新', () => {
    expect(shouldReloadAfterUpgrade('succeeded', 'req-1', '1.0.7', '1.0.7', null)).toBe(true);
    expect(shouldReloadAfterUpgrade('succeeded', 'req-1', '1.0.7', '1.0.7', 'req-1')).toBe(false);
    expect(shouldReloadAfterUpgrade('verifying', 'req-1', '1.0.6', '1.0.7', null)).toBe(false);
  });
});
