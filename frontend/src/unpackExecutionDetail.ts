import type { UnpackItemDiagnostics, UnpackOperationDiagnostic } from './api/unpack';
import {
  unpackErrorLabel,
  unpackOperationStatusLabel,
  unpackOperationTypeLabel,
  unpackRecordedStateLabel,
} from './unpackPresentation';

export type ExecutionDetailStep = {
  key: string;
  label: string;
  result: 'recorded' | 'attention' | 'failed';
  note: string;
  at: string | null;
};

/** A journal is a historical record, never proof of current disk or client state. */
function operationFamily(type: string): string {
  if (type === 'UNPACK_EXEC_CREATE_DIRECTORY' || type.startsWith('UNPACK_EXEC_MATERIALIZE_'))
    return 'files';
  if (type.endsWith('_ADD')) return 'add';
  if (type.endsWith('_VERIFY') || type.endsWith('_RECHECK')) return 'verify';
  if (type.endsWith('_START')) return 'start';
  if (type.endsWith('_STOP')) return 'stop';
  if (type.endsWith('_REMOVE')) return 'remove';
  return type;
}

function familyLabel(family: string, record: UnpackOperationDiagnostic): string {
  const labels: Record<string, string> = {
    files: '准备目标文件',
    add: '向下载器添加种子',
    verify: '请求下载器校验',
    start: '请求开始下载或做种',
    stop: '请求停止下载',
    remove: '清理下载器任务',
  };
  return labels[family] || unpackOperationTypeLabel(record.operation_type);
}

/** Groups only consecutive recorded effects. No invented VERIFY, START or success step. */
export function unpackExecutionDetailTimeline(
  detail: UnpackItemDiagnostics,
): ExecutionDetailStep[] {
  const entries = [...detail.operations].sort((a, b) => a.updated_at.localeCompare(b.updated_at));
  const groups: Array<{ family: string; entries: UnpackOperationDiagnostic[] }> = [];
  for (const entry of entries) {
    const family = operationFamily(entry.operation_type);
    const last = groups[groups.length - 1];
    if (last?.family === family) last.entries.push(entry);
    else groups.push({ family, entries: [entry] });
  }
  const stages: ExecutionDetailStep[] = groups.map(({ family, entries }, index) => {
    const allApplied = entries.every((row) => row.status === 'APPLIED');
    const failed = entries.some((row) => row.status === 'FAILED');
    const result = allApplied ? 'recorded' : failed ? 'failed' : 'attention';
    const note =
      result === 'recorded'
        ? '过去记录了 ' + entries.length + ' 次操作成功，当前状态仍需核实'
        : result === 'attention'
          ? '有操作的实际结果尚未确认，不能直接重复执行'
          : '有操作在当时报告失败，请检查记录';
    return {
      key: family + '-' + index,
      label: familyLabel(family, entries[0]!),
      result,
      note,
      at: entries[entries.length - 1]?.updated_at ?? null,
    };
  });
  if (detail.last_error_code) {
    stages.push({
      key: 'reported-error',
      label: '任务报告错误',
      result: 'failed',
      note: unpackErrorLabel(detail.last_error_code, detail.last_error_message),
      at: null,
    });
  }
  return stages;
}

export function unpackExecutionDetailSummary(detail: UnpackItemDiagnostics): {
  title: string;
  description: string;
  recommendation: string;
  recommendationLabel: string;
} {
  const error = unpackErrorLabel(detail.last_error_code, detail.last_error_message);
  const history = detail.operations.length
    ? '之前的文件和下载器操作已有记录，但这些记录不能证明当前状态。'
    : '没有记录到文件或下载器操作。';
  const recommendationLabel = detail.requires_manual_reconciliation
    ? '先人工核对'
    : detail.last_error_code
      ? '先检查失败原因'
      : '查看操作记录';
  if (detail.last_error_code === 'UNPACK_SEED_SKIP_CHECK_NOT_COMPLETE') {
    return {
      title: '下载器尚未确认种子已完整下载并停止',
      description: '程序为防止未完成的文件被直接用于做种，停止了后续操作。' + history,
      recommendation:
        '请先只读查询下载器状态，确认文件和种子归属。若当前状态仍不明确，请人工检查，不要直接重复添加种子。',
      recommendationLabel,
    };
  }
  if (detail.last_error_code === 'SITE_INVALID_RESPONSE') {
    return {
      title: '站点返回的种子信息有问题',
      description:
        '获取种子或生成执行计划时，站点返回了无法正常使用的数据，因此任务没有继续。' + history,
      recommendation: detail.requires_manual_reconciliation
        ? '先核对已有的文件和下载器操作，再检查站点是否恢复正常。'
        : '检查站点或种子详情是否可以正常访问，确认后再考虑重试。',
      recommendationLabel,
    };
  }
  if (detail.last_error_code) {
    return {
      title: '任务在执行过程中遇到了问题',
      description: error + '。' + history,
      recommendation: detail.requires_manual_reconciliation
        ? '先人工确认之前的操作结果，再决定能否继续，避免重复写入。'
        : '先检查失败原因。已有外部操作时，还应核对下载器和文件是否仍正常。',
      recommendationLabel,
    };
  }
  return {
    title: '以下是这部影片的历史执行情况',
    description: history + '这里只显示程序留下的记录，不会重新执行操作。',
    recommendation: detail.requires_manual_reconciliation
      ? '有操作结果尚未确认，请先核对后再采取操作。'
      : '如需了解下载器现在的状态，可使用下方只读查询。',
    recommendationLabel,
  };
}

export function unpackExecutionDetailRecordNote(operation: UnpackOperationDiagnostic): string {
  const identity = unpackRecordedStateLabel(
    operation.last_observed_state,
    operation.recorded_file_type,
  );
  const failure = operation.last_error_code
    ? '；' + unpackErrorLabel(operation.last_error_code, null)
    : '';
  return (
    (identity === '未记录' ? unpackOperationStatusLabel(operation.status) : identity) + failure
  );
}
