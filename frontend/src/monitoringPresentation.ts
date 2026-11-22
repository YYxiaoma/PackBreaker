import type { SeedingIssueRow } from './api/monitoring';

export const ISSUE_KIND_LABELS: Record<string, string> = {
  SITE_MISSING: '站侧无记录',
  SITE_ONLY: '站侧有但下载器没有',
  TRACKER_ERROR: 'Tracker 持续报错',
  FILE_MISSING: '本地文件缺失',
};

export const SUGGESTION_LABELS: Record<string, string> = {
  RETRIEVE_TORRENT: '重新拉取 .torrent',
  RECHECK: '强制重校验',
  RESEED: '重建辅种',
  NONE: '暂不处理',
};

export const STATUS_LABELS: Record<string, string> = {
  OPEN: '待处理',
  ACKED: '已确认',
  REPAIRING: '修复中',
  RESOLVED: '已恢复',
  IGNORED: '已忽略',
};

export type SeverityTone = 'danger' | 'warning' | 'info';

export function severityLabel(severity: string): string {
  if (severity === 'HIGH') return '高风险';
  if (severity === 'MEDIUM') return '中风险';
  return '低风险';
}

export function severityTone(severity: string): SeverityTone {
  if (severity === 'HIGH') return 'danger';
  if (severity === 'MEDIUM') return 'warning';
  return 'info';
}

export function kindLabel(kind: string): string {
  return ISSUE_KIND_LABELS[kind] ?? kind;
}

export function suggestionLabel(suggestion: string): string {
  return SUGGESTION_LABELS[suggestion] ?? suggestion;
}

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value <= 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

export function hoursText(hours: number | null | undefined): string {
  // 未知规则不等于 0 风险，必须显示为未知。
  if (hours === null || hours === undefined) return '未知';
  if (hours <= 0) return '已满足';
  if (hours < 24) return `剩余 ${hours.toFixed(0)} 小时`;
  return `剩余 ${(hours / 24).toFixed(1)} 天`;
}

export function evidenceSummary(row: SeedingIssueRow): string {
  if (row.kind === 'TRACKER_ERROR') return 'tracker 连续通告失败';
  if (row.kind === 'FILE_MISSING') {
    return row.affected_torrents > 1
      ? `内容路径在容器内不存在（影响 ${row.affected_torrents} 个种子）`
      : '内容路径在容器内不存在';
  }
  if (row.kind === 'SITE_ONLY') return '站点做种列表存在，下载器中没有对应任务';
  return `站点详情页 404，连续 ${row.consecutive_misses} 次未找到`;
}

export function issueQueryFromTab(tab: string): { kind?: string } {
  return tab === 'ALL' ? {} : { kind: tab };
}
