import { describe, expect, it } from 'vitest';

import {
  BACKGROUND_RELEASE_CHECK_INTERVAL_MS,
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
});
