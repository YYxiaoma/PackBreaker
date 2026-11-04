import { describe, expect, it } from 'vitest';
import { formatSiteBytes, formatSiteDate, formatSiteRatio } from './siteStats';

describe('站点统计格式化', () => {
  it('使用 IEC 单位并保留两位精度，与 PT-depiler 的 GiB/TiB 口径一致', () => {
    expect(formatSiteBytes(5.6 * 1024 ** 4)).toBe('5.60 TiB');
    expect(formatSiteBytes(62.94 * 1024 ** 3)).toBe('62.94 GiB');
    expect(formatSiteBytes(139.33 * 1024 ** 3)).toBe('139.33 GiB');
  });

  it('格式化加入时间和最后访问时间，并安全处理缺失或无效日期', () => {
    expect(formatSiteDate('2026-08-07T11:56:49Z')).not.toBe('—');
    expect(formatSiteDate('2026-10-16T01:18:09Z')).not.toBe('—');
    expect(formatSiteDate(null)).toBe('—');
    expect(formatSiteDate('not-a-date')).toBe('—');
  });

  it('上传量大于零且下载量为零时显示无穷大分享率', () => {
    expect(formatSiteRatio(null, 1024, 0)).toBe('∞');
    expect(formatSiteRatio(91.05, 1024, 0)).toBe('91.05');
    expect(formatSiteRatio(null, 0, 0)).toBe('—');
  });
});
