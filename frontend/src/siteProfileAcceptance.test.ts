import { describe, expect, it } from 'vitest';

import type { SiteUserProfile } from './api/sites';
import { siteProfileAcceptanceReport } from './siteProfileAcceptance';

const synthetic: SiteUserProfile = {
  site_id: 'hhclub',
  uid: 'SYNTHETIC-SENSITIVE-UID-009',
  username: 'SYNTHETIC-SENSITIVE-USERNAME',
  user_level: 'SYNTHETIC-SENSITIVE-LEVEL',
  real_uploaded_bytes: 24811,
  real_downloaded_bytes: 14099,
  uploaded_bytes: 28123,
  downloaded_bytes: 18900,
  ratio: 1.234,
  torrents_posted: 0,
  seeding_count: null,
  seeding_size_bytes: 510240,
  bonus: 21.44,
  seeding_points: null,
  bonus_per_hour: 0.0004,
  fetched_at: '2026-09-21T12:00:00Z',
};

describe('三站点脱敏字段验收复制', () => {
  it('只输出字段状态，区分真实零值、微小正值及缺失', () => {
    const report = siteProfileAcceptanceReport('HHCLUB', synthetic);
    expect(report).toContain('站点类型：HHCLUB');
    expect(report).toContain('发种数：真实零值');
    expect(report).toContain('做种数：缺失');
    expect(report).toContain('做种量：已取得');
    expect(report).toContain('每小时魔力值：已取得');
    expect(report).toContain('备注：仅字段可读取性；不代表站点页面同一时点数值对账');
    for (const secret of [
      synthetic.uid,
      synthetic.username,
      synthetic.user_level,
      synthetic.seeding_size_bytes,
      synthetic.bonus,
      synthetic.bonus_per_hour,
      synthetic.fetched_at,
    ]) {
      expect(report).not.toContain(String(secret));
    }
  });

  it('账号身份不明时不导出表面完整的六项字段', () => {
    const report = siteProfileAcceptanceReport('MTEAM', { ...synthetic, uid: null });
    expect(report).toContain('当前账号身份未确认');
    expect(report).toContain('六项目标字段：未验收');
    expect(report).not.toContain('发种数：真实零值');
  });

  it.each(['1024', ' 1024 \t', ''])('数字等级或空等级 %j 不得误报为已取得名称', (level) => {
    const report = siteProfileAcceptanceReport('HDTIME', { ...synthetic, user_level: level });
    expect(report).toContain('用户等级：缺失');
    expect(report).not.toContain('用户等级：已取得');
    expect(report).not.toContain(level.trim() || 'SYNTHETIC-SENSITIVE-LEVEL');
  });

  it('失败时只允许稳定 SITE_ 错误代码，不复制任意异常正文或 trace ID', () => {
    const good = siteProfileAcceptanceReport('HDTIME', null, 'SITE_UNAVAILABLE');
    expect(good).toContain('SITE_UNAVAILABLE');
    expect(good).toContain('六项目标字段：未验收');
    const injected = siteProfileAcceptanceReport('HDTIME', null, 'SITE_UNAVAILABLE Cookie=PRIVATE');
    expect(injected).toContain('UNCONFIRMED');
    expect(injected).not.toContain('PRIVATE');
  });

  it('非验收站点仅输出 OTHER，不传播任意站点配置名称', () => {
    expect(siteProfileAcceptanceReport('KEEPFRDS', null)).toContain('站点类型：OTHER');
  });
});
