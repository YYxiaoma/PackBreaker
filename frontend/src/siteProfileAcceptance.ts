import type { SiteKind, SiteUserProfile } from './api/sites';

/**
 * Generates a deliberately allowlisted report for manually sharing the
 * readability of current-account site statistics. No account identifiers,
 * site names, metric values, raw HTTP responses, or trace IDs are copied.
 * A field's presence is NOT a same-time comparison with the original site.
 */
const FIELDS = [
  ['用户等级', 'user_level'],
  ['发种数', 'torrents_posted'],
  ['做种数', 'seeding_count'],
  ['做种量', 'seeding_size_bytes'],
  ['每小时魔力值', 'bonus_per_hour'],
  ['做种积分', 'seeding_points'],
] as const;

type AllowedKind = Extract<SiteKind, 'MTEAM' | 'HHCLUB' | 'HDTIME'>;

function reportSite(kind: SiteKind): AllowedKind | 'OTHER' {
  return kind === 'MTEAM' || kind === 'HHCLUB' || kind === 'HDTIME' ? kind : 'OTHER';
}

export function siteProfileAcceptanceReport(
  kind: SiteKind,
  profile: SiteUserProfile | null | undefined,
  errorCode?: string | null,
): string {
  const lines = [`站点类型：${reportSite(kind)}`];
  if (!profile) {
    const safeCode =
      errorCode && /^SITE_[A-Z0-9_]{1,48}$/.test(errorCode) ? errorCode : 'UNCONFIRMED';
    lines.push(`读取状态：失败或未确认（${safeCode}）`, '六项目标字段：未验收');
  } else if (!profile.uid?.trim()) {
    lines.push('读取状态：当前账号身份未确认', '六项目标字段：未验收');
  } else {
    lines.push('读取状态：当前账号已确认（仅字段可读取性）');
    for (const [label, field] of FIELDS) {
      const value = profile[field];
      // The details view refuses numeric-only rank IDs: they are not a
      // confirmed human-readable level name and cannot pass field acceptance.
      const unconfirmedLevel =
        field === 'user_level' && typeof value === 'string' && /^\d+$/.test(value.trim());
      const state =
        value === null ||
        value === undefined ||
        (typeof value === 'string' && !value.trim()) ||
        unconfirmedLevel
          ? '缺失'
          : value === 0
            ? '真实零值'
            : '已取得';
      lines.push(`${label}：${state}`);
    }
  }
  lines.push('备注：仅字段可读取性；不代表站点页面同一时点数值对账或版本发布验收。');
  return lines.join('\n');
}
