import type { UnpackDefinition, UnpackExecutionStatus, UnpackItemStatus } from './api/unpack';

export type UnpackDefinitionStatus = UnpackDefinition['status'];
export type UnpackTriggerKind = UnpackDefinition['trigger_kind'];
export type UnpackSourceKind = UnpackDefinition['source_kind'];

const DEFINITION_STATUS_LABELS: Record<UnpackDefinitionStatus, string> = {
  PENDING_EXECUTION: '待执行',
  ENABLED: '已启用',
  PAUSED: '已暂停',
  ERROR: '异常',
};

const EXECUTION_STATUS_LABELS: Record<UnpackExecutionStatus, string> = {
  DISCOVERING: '获取影片',
  MATCHING: '匹配中',
  REVIEW_REQUIRED: '待人工审核',
  CONTENT_VERIFYING: '内容校验中',
  EXECUTING: '执行中',
  CLIENT_VERIFYING: '下载器校验中',
  COMPLETED: '已完成',
  COMPLETED_WITH_ERRORS: '部分完成',
  PAUSED: '已暂停',
  FAILED: '执行失败',
  CANCELLED: '已取消',
};

const ITEM_STATUS_LABELS: Record<UnpackItemStatus, string> = {
  DISCOVERED: '已发现',
  MATCH_PENDING: '待匹配',
  MATCHING: '匹配中',
  MATCHED_AUTO: '自动匹配成功',
  REVIEW_REQUIRED: '待人工审核',
  MATCHED_MANUAL: '人工匹配成功',
  NO_MATCH: '无匹配',
  MATCH_TIMEOUT: '匹配超时',
  MATCH_ERROR: '匹配错误',
  TORRENT_FETCHING: '获取种子中',
  AUXILIARY_FETCHING: '补齐辅助文件',
  CONTENT_VERIFYING: '内容校验中',
  CONTENT_VERIFIED: '内容校验通过',
  CONTENT_MISMATCH: '内容不一致',
  PLAN_PENDING: '执行计划就绪',
  EXECUTING: '执行中',
  CLIENT_VERIFYING: '下载器校验中',
  COMPLETED: '已完成',
  EXECUTION_ERROR: '执行错误',
  CANCELLED: '已取消',
};

export function unpackDefinitionStatusLabel(value: UnpackDefinitionStatus): string {
  return DEFINITION_STATUS_LABELS[value];
}

export function unpackExecutionStatusLabel(value: UnpackExecutionStatus): string {
  return EXECUTION_STATUS_LABELS[value];
}

export function unpackItemStatusLabel(value: UnpackItemStatus): string {
  return ITEM_STATUS_LABELS[value];
}

export function unpackTriggerLabel(value: UnpackTriggerKind): string {
  return value === 'MANUAL' ? '手动拆包' : '监控拆包';
}

export function unpackSourceKindLabel(value: UnpackSourceKind): string {
  return value === 'DIRECTORY' ? '目录' : '下载器';
}

export function unpackVerificationLevelLabel(value: string | null): string {
  if (value === 'FULL_VERIFIED') return '完整内容校验通过';
  if (value === 'CLIENT_CHECK_REQUIRED') return '需要下载器校验';
  if (value === 'BLOCKED') return '内容校验阻断';
  return '尚未校验';
}

export function unpackErrorLabel(code: string | null, message: string | null): string {
  if (message?.trim()) return message.trim();
  const known: Record<string, string> = {
    UNPACK_MATCH_TIMEOUT: '站点匹配超时，可重试该影片',
    UNPACK_MATCH_ERROR: '影片匹配失败，可重试该影片',
    UNPACK_EXECUTION_PLAN_BLOCKED: '执行计划被安全检查阻断',
    UNPACK_SEED_CLIENT_VERIFY_FAILED: '下载器校验未通过，禁止启动做种',
    UNPACK_SEED_RECONCILE_REQUIRED: '外部操作结果需要人工对账',
    SOURCE_CHANGED: '源文件已发生变化，请重新执行',
    TARGET_CONFLICT: '目标位置存在冲突，未执行覆盖',
  };
  return (code && known[code]) || '暂无错误信息';
}

export function unpackProgressPercent(totalCount: number, completedCount: number): number {
  if (totalCount <= 0) return 0;
  return Math.min(100, Math.max(0, Math.round((completedCount / totalCount) * 100)));
}
