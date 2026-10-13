import type {
  UnpackDefinition,
  UnpackExecutionItem,
  UnpackItemDiagnostics,
  UnpackLiveClientRead,
  UnpackExecutionStatus,
  UnpackItemStatus,
} from './api/unpack';

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

export function unpackItemExternalRiskLabel(
  item: Pick<UnpackExecutionItem, 'status' | 'has_external_operations' | 'last_error_code'>,
): string | null {
  if (
    item.status !== 'EXECUTION_ERROR' ||
    !item.has_external_operations ||
    ![
      'UNPACK_EXEC_MATERIALIZATION_PROOF_MISSING',
      'UNPACK_EXEC_MATERIALIZATION_TARGET_DRIFT',
      'UNPACK_SEED_RECONCILE_REQUIRED',
    ].includes(item.last_error_code ?? '')
  ) {
    return null;
  }
  // The item-list contract does not expose execution_state/STARTING. Warn
  // conservatively instead of inventing a precise client state from status.
  return '已存在外部操作，下载器可能已启动；物化文件或操作证据异常，必须人工对账，禁止普通重试、重新添加种子或删除操作记录。';
}

/** Evidence-based read-only checklist. Nothing here approves or clears a journal. */
export function unpackReconciliationGuidance(
  historical: Pick<
    UnpackItemDiagnostics,
    'reconciliation_reasons' | 'requires_manual_reconciliation' | 'operations_truncated'
  >,
  live: Pick<UnpackLiveClientRead, 'status' | 'error_code'> | null,
): string[] {
  const steps: string[] = [];
  const reasons = new Set(historical.reconciliation_reasons);
  if (reasons.has('JOURNAL_RECONCILE_REQUIRED')) {
    steps.push(
      '存在待人工对账的外部流水：核对操作类型、最后错误和实际客户端结果，不要覆盖或清空 journal。',
    );
  }
  if (reasons.has('EXTERNAL_WRITE_UNCONFIRMED')) {
    steps.push(
      '失败任务存在未确认外部写入：先确认请求是否到达下载器，再决定后续处理；不能自动重试或重发。',
    );
  }
  if (reasons.has('MATERIALIZATION_EVIDENCE_INVALID')) {
    steps.push(
      '冻结的物化文件身份或流水证明异常：人工核对原文件、目标文件及 inode/链接关系，不可直接覆盖媒体。',
    );
  }
  if (reasons.has('MULTIPLE_DOWNLOADER_ADD_PROOFS') || live?.error_code === 'ADD_PROOF_AMBIGUOUS') {
    steps.push(
      '同一影片存在 qBittorrent（qB）与 Transmission 两套 ADD 流水：不能自动选择、认领或修改任一客户端下载任务，须人工核对两端操作与冻结计划。',
    );
  }
  if (live === null) {
    steps.push('尚未执行本次实时只读核验；历史流水不能代表当前下载器状态。');
  } else if (live.status === 'MATCHED') {
    steps.push(
      '此次查询确认 Hash、保存路径与归属标签匹配，但不代表对账完成，也不证明目标媒体文件仍完整。',
    );
  } else if (live.status === 'MISSING') {
    steps.push(
      '此次查询未找到目标种子：需人工核对下载器连接、冻结 Hash 与任务变动，不得据此自动重新添加。',
    );
  } else if (live.status === 'OWNERSHIP_MISMATCH') {
    steps.push(
      '发现种子归属冲突：人工核对 Hash、保存路径与标签，禁止接管或移除疑似其他程序的种子。',
    );
  } else if (live.status === 'UNRESOLVED_ADD') {
    steps.push('ADD 请求结果未获可信确认；为避免误认领，本次未查询或接管下载器中的同 Hash 种子。');
  } else if (live.status === 'NO_ADD_EVIDENCE') {
    steps.push('冻结的 ADD 成功证明不存在；未执行下载器核验，不能推断外部没有该种子。');
  } else {
    steps.push('下载器状态未知或查询时证据发生变化；不能据此认定种子已丢失或已安全做种。');
  }
  if (historical.operations_truncated) {
    steps.push('操作记录列表已截断；必须人工核对全部 journal，未显示的流水也可能存在冲突。');
  }
  if (historical.requires_manual_reconciliation) {
    steps.push(
      '必须人工核对并保留完整证据；实时只读结果不会解除安全拦截、自动清除对账状态或自动执行写操作。',
    );
  }
  return steps;
}

export function unpackProgressPercent(totalCount: number, completedCount: number): number {
  if (totalCount <= 0) return 0;
  return Math.min(100, Math.max(0, Math.round((completedCount / totalCount) * 100)));
}
