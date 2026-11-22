import { describe, expect, it } from 'vitest';

import type { SeedingIssueRow } from './api/monitoring';
import {
  evidenceSummary,
  formatBytes,
  hoursText,
  kindLabel,
  severityLabel,
  severityTone,
  statusLabel,
  suggestionLabel,
} from './monitoringPresentation';

function row(overrides: Partial<SeedingIssueRow> = {}): SeedingIssueRow {
  return {
    id: 'issue-1',
    kind: 'SITE_MISSING',
    kind_label: '站侧无记录',
    severity: 'HIGH',
    risk_score: 80,
    status: 'OPEN',
    suggestion: 'RETRIEVE_TORRENT',
    detect_count: 2,
    first_detected_at: '2026-11-18T09:30:00+08:00',
    last_detected_at: '2026-11-20T09:30:00+08:00',
    torrent_name: 'Dune Part Two 2024 2160p',
    torrent_hash: 'a'.repeat(40),
    site_key: 'mteam',
    downloader_id: 'dl-1',
    size_bytes: 50_000_000_000,
    consecutive_misses: 3,
    affected_torrents: 1,
    ...overrides,
  };
}

describe('monitoring presentation', () => {
  it('maps severity to Chinese labels and tones', () => {
    expect(severityLabel('HIGH')).toBe('高风险');
    expect(severityLabel('MEDIUM')).toBe('中风险');
    expect(severityLabel('LOW')).toBe('低风险');
    expect(severityTone('HIGH')).toBe('danger');
    expect(severityTone('MEDIUM')).toBe('warning');
    expect(severityTone('LOW')).toBe('info');
  });

  it('maps issue kinds and suggestions with fallbacks', () => {
    expect(kindLabel('SITE_MISSING')).toBe('站侧无记录');
    expect(kindLabel('UNKNOWN_KIND')).toBe('UNKNOWN_KIND');
    expect(suggestionLabel('RECHECK')).toBe('强制重校验');
    expect(statusLabel('REPAIRING')).toBe('修复中');
  });

  it('formats byte sizes and handles unknown values', () => {
    expect(formatBytes(0)).toBe('—');
    expect(formatBytes(null)).toBe('—');
    expect(formatBytes(1536)).toBe('1.5 KB');
    expect(formatBytes(50_000_000_000)).toBe('46.6 GB');
  });

  it('never reports unknown H&R rules as zero risk', () => {
    expect(hoursText(null)).toBe('未知');
    expect(hoursText(undefined)).toBe('未知');
    expect(hoursText(0)).toBe('已满足');
    expect(hoursText(10)).toBe('剩余 10 小时');
    expect(hoursText(48)).toBe('剩余 2.0 天');
  });

  it('builds a one-line evidence summary per kind', () => {
    expect(evidenceSummary(row())).toBe('站点详情页 404，连续 3 次未找到');
    expect(evidenceSummary(row({ kind: 'TRACKER_ERROR' }))).toBe('tracker 连续通告失败');
    expect(evidenceSummary(row({ kind: 'FILE_MISSING' }))).toBe('内容路径在容器内不存在');
  });
});
