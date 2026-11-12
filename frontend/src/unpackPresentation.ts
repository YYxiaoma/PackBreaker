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
  CLIENT_VERIFYING: '下载器处理中',
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
  CONTENT_VERIFY_ERROR: '校验错误',
  TORRENT_FETCHING: '获取种子中',
  AUXILIARY_FETCHING: '补齐辅助文件',
  CONTENT_VERIFYING: '内容校验中',
  CONTENT_VERIFIED: '内容校验通过',
  CONTENT_MISMATCH: '内容不一致',
  PLAN_PENDING: '执行计划待处理',
  EXECUTING: '执行中',
  CLIENT_VERIFYING: '下载器处理中',
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

export function unpackItemStatusSummary(
  item: Pick<UnpackExecutionItem, 'status' | 'last_error_code'>,
): string {
  if (item.status === 'PLAN_PENDING' && item.last_error_code === 'UNPACK_EXECUTION_PLAN_BLOCKED') {
    return '执行计划已阻断';
  }
  return unpackItemStatusLabel(item.status);
}

export function unpackPlanStatusLabel(value: UnpackItemDiagnostics['plan_status']): string {
  const labels: Record<UnpackItemDiagnostics['plan_status'], string> = {
    NOT_CREATED: '当时还没有生成计划',
    READY: '当时的检查通过（现在未复查）',
    BLOCKED: '当时发现问题，未继续执行',
    INVALID: '保存的执行安排无效，需要检查',
  };
  return labels[value];
}

export function unpackPlanBlockReasonLabel(value: string): string {
  const labels: Record<string, string> = {
    TARGET_PARENT_UNSAFE: '目标目录不存在或不安全',
    TARGET_EXISTS: '目标文件存在冲突',
    CROSS_DEVICE: '跨设备硬链接不被允许',
    MAPPING_UNSUPPORTED: '文件映射证据不完整或已变化',
  };
  return labels[value] ?? '其他安全阻断';
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
  if (code === 'UNPACK_SEED_SKIP_CHECK_NOT_COMPLETE')
    return '下载器未确认种子已经完整下载且处于停止状态，因此没有启动做种。请检查下载器后重试。';
  if (code === 'SITE_INVALID_RESPONSE')
    return message?.includes('执行计划')
      ? '站点返回的数据异常，无法生成执行计划。可以稍后重试。'
      : '站点返回的种子信息异常，未能继续做种。可以稍后重试。';
  if (message?.trim()) return message.trim();
  const known: Record<string, string> = {
    UNPACK_MATCH_TIMEOUT: '站点匹配超时，可重试该影片',
    UNPACK_MATCH_ERROR: '影片匹配失败，可重试该影片',
    UNPACK_VERIFY_TORRENT_FETCH_TIMEOUT: '获取候选 torrent 超时，可重试内容校验',
    SITE_RISK_CONTROL_LIMIT_REACHED: '站点在 10 分钟内触发风控次数上限，已自动停用',
    SITE_RISK_CONTROL_COOLDOWN: '站点触发风控，任务已按配置间隔延后重试',
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
      '有一项文件或下载器操作的结果还没确认。先检查它是否真的完成，不要删除这些历史记录。',
    );
  }
  if (reasons.has('EXTERNAL_WRITE_UNCONFIRMED')) {
    steps.push(
      '有一项请求的结果还没有确认。请先确认请求是否到达下载器、种子是否已经存在，不能自动重试或重复发送。',
    );
  }
  if (reasons.has('MATERIALIZATION_EVIDENCE_INVALID')) {
    steps.push(
      '准备文件时的记录可能与实际文件不一致。请人工核对原文件和目标文件是否被移动或修改，不能直接覆盖文件。',
    );
  }
  if (reasons.has('MULTIPLE_DOWNLOADER_ADD_PROOFS') || live?.error_code === 'ADD_PROOF_AMBIGUOUS') {
    steps.push(
      '同一影片在 qBittorrent（qB）和 Transmission 都留下过添加种子的记录。不能自动选择其中一个，请人工核对两边是否重复做种，不要擅自删除。',
    );
  }
  if (live === null) {
    steps.push('本次尚未执行下载器查询。下面的历史记录不能代表下载器现在的情况。');
  } else if (live.status === 'MATCHED') {
    steps.push(
      '这次查询在下载器中找到了属于本任务的种子，但还不能保证磁盘文件完整，也不能直接解除风险限制。',
    );
  } else if (live.status === 'MISSING') {
    steps.push(
      '本次在下载器中未找到对应种子。请先检查连接和任务是否被移动或删除，不要直接重新添加。',
    );
  } else if (live.status === 'OWNERSHIP_MISMATCH') {
    steps.push(
      '发现种子归属冲突：这可能是其他程序创建的种子。请核对保存位置和任务标记，不要直接接管或删除。',
    );
  } else if (live.status === 'UNRESOLVED_ADD') {
    steps.push(
      '以前发出过添加种子的请求，但结果没有确认。为避免重复添加或接管错误的种子，本次没有继续操作。',
    );
  } else if (live.status === 'NO_ADD_EVIDENCE') {
    steps.push(
      '没有找到以前成功添加种子的可靠记录，因此这次没查询下载器；这不代表下载器里一定没有该种子。',
    );
  } else {
    steps.push('下载器当前状态未知，或查询时记录发生了变化。无法确认种子是否安全运行。');
  }
  if (historical.operations_truncated) {
    steps.push('操作记录较多，这里只展示了一部分。未显示的记录也可能需要人工检查。');
  }
  if (historical.requires_manual_reconciliation) {
    steps.push(
      '请人工核对并保留这些记录。本页的查询只读，不会解除安全限制，也不会自动修改文件或种子。',
    );
  }
  return steps;
}

export function unpackExecutionStageLabel(stage: string | null): string {
  const names: Record<string, string> = {
    FILES_MATERIALIZED: '文件已准备好',
    ADD_RESUME_CHECKING: '等待 qBittorrent 恢复种子数据（不执行完整文件校验）',
    TORRENT_ADDED: '种子已添加到下载器',
    CLIENT_VERIFYING: '等待下载器完成文件校验',
    CLIENT_VERIFIED: '下载器文件校验已完成',
    STARTING: '准备启动做种',
    COMPLETED: '已经完成',
  };
  return (stage && names[stage]) || '尚未进入最终做种步骤';
}

export function unpackOperationTypeLabel(type: string): string {
  const names: Record<string, string> = {
    UNPACK_EXEC_CREATE_DIRECTORY: '创建目标文件夹',
    UNPACK_EXEC_MATERIALIZE_HARDLINK: '通过硬链接准备文件',
    UNPACK_EXEC_MATERIALIZE_SYMLINK: '通过软链接准备文件',
    UNPACK_EXEC_MATERIALIZE_COPY: '复制文件到目标目录',
    UNPACK_EXEC_QBITTORRENT_ADD: '向 qBittorrent 添加种子',
    UNPACK_EXEC_QBITTORRENT_RECHECK: '让 qBittorrent 校验文件',
    UNPACK_EXEC_QBITTORRENT_START: '在 qBittorrent 启动做种',
    UNPACK_EXEC_TRANSMISSION_ADD: '向 Transmission 添加种子',
    UNPACK_EXEC_TRANSMISSION_VERIFY: '让 Transmission 校验文件',
    UNPACK_EXEC_TRANSMISSION_START: '在 Transmission 启动做种',
    UNPACK_AUX_STAGING_DIR: '创建辅助文件目录',
    UNPACK_AUX_TORRENT_ADD: '添加辅助文件下载',
    UNPACK_AUX_FILE_SELECTION: '选择所需辅助文件',
    UNPACK_AUX_TORRENT_START: '启动辅助文件下载',
    UNPACK_AUX_TORRENT_STOP: '停止辅助文件下载',
    UNPACK_AUX_TORRENT_REMOVE: '清理辅助文件下载',
  };
  return names[type] || '其他文件或下载器操作';
}

export function unpackOperationStatusLabel(status: string): string {
  const names: Record<string, string> = {
    APPLIED: '已记录成功',
    INTENT_RECORDED: '结果尚未确认',
    RECONCILE_REQUIRED: '需要人工检查',
    FAILED: '失败',
    CANCELLED: '已取消',
  };
  return names[status] || '结果不明确';
}

export function unpackRecordedStateLabel(state: string | null, fileType: string | null): string {
  const files: Record<string, string> = {
    directory: '文件夹',
    regular: '文件',
    symlink: '软链接',
  };
  if (fileType) return files[fileType] || '文件操作记录';
  if (!state) return '未记录';
  const value = state.toLowerCase();
  if (/stop|pause/.test(value)) return '已停止或暂停';
  if (/seed|upload/.test(value)) return '做种中';
  if (/check|verify/.test(value)) return '校验中';
  if (/download/.test(value)) return '下载中';
  return '下载器状态记录';
}

export function unpackLiveStateLabel(state: string | null): string {
  return unpackRecordedStateLabel(state, null);
}

export function unpackItemRetryAvailability(
  item: Pick<UnpackExecutionItem, 'retry_capability'>,
): 'match' | 'verification' | 'plan' | 'resume' | 'reconcile' | 'none' {
  const capabilities: Record<
    UnpackExecutionItem['retry_capability'],
    'match' | 'verification' | 'plan' | 'resume' | 'reconcile' | 'none'
  > = {
    MATCH: 'match',
    VERIFICATION: 'verification',
    PLAN: 'plan',
    RESUME: 'resume',
    RECONCILE: 'reconcile',
    NONE: 'none',
  };
  return capabilities[item.retry_capability] ?? 'none';
}

export function unpackProgressPercent(totalCount: number, completedCount: number): number {
  if (totalCount <= 0) return 0;
  return Math.min(100, Math.max(0, Math.round((completedCount / totalCount) * 100)));
}
