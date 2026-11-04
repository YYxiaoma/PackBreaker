import { describe, expect, it } from 'vitest';
import type { UnpackItemDiagnostics, UnpackOperationDiagnostic } from './api/unpack';
import {
  unpackExecutionDetailTimeline,
  unpackExecutionDetailSummary,
  unpackExecutionDetailRecordNote,
} from './unpackExecutionDetail';

const stamp = '2026-10-23T05:00:00Z';
function operation(
  type: string,
  status: string = 'APPLIED',
  at: string = stamp,
): UnpackOperationDiagnostic {
  return {
    operation_type: type,
    status,
    last_error_code: null,
    downloader_id: null,
    torrent_hash: null,
    target_path: '/isolated/safe/test',
    last_observed_state: null,
    recorded_file_type: 'regular',
    recorded_device: 100,
    recorded_inode: 200,
    recorded_size: 1024,
    updated_at: at,
  };
}
function diagnostics(changes: Partial<UnpackItemDiagnostics> = {}): UnpackItemDiagnostics {
  return {
    item_id: 'item-1',
    status: 'EXECUTION_ERROR',
    item_version: 4,
    execution_stage: 'FILES_MATERIALIZED',
    plan_status: 'READY',
    plan_blocked_reasons: [],
    acceptance_read_only: false,
    last_error_code: 'SITE_INVALID_RESPONSE',
    last_error_message: '最终辅种获取 torrent 失败',
    requires_manual_reconciliation: false,
    reconciliation_reasons: [],
    downloader_state_live_checked: false,
    filesystem_state_live_checked: false,
    operations_truncated: false,
    operations: [
      operation('UNPACK_EXEC_CREATE_DIRECTORY', 'APPLIED', '2026-10-23T05:00:00Z'),
      operation('UNPACK_EXEC_MATERIALIZE_HARDLINK', 'APPLIED', '2026-10-23T05:00:01Z'),
      operation('UNPACK_EXEC_MATERIALIZE_HARDLINK', 'APPLIED', '2026-10-23T05:00:02Z'),
    ],
    ...changes,
  };
}
describe('scheme A execution detail from historical evidence', () => {
  it('groups recorded materialization without fabricating downloader operations', () => {
    const steps = unpackExecutionDetailTimeline(diagnostics());
    expect(steps.map((x) => x.label)).toEqual(['准备目标文件', '任务报告错误']);
    expect(steps[0]?.result).toBe('recorded');
    expect(steps[0]?.note).toContain('3 次操作成功');
    expect(steps[1]?.result).toBe('failed');
    expect(steps[1]?.at).toBeNull();
    expect(steps.map((x) => x.label).join(' ')).not.toContain('下载器校验');
  });
  it('uses ADD evidence only when the journal contains an ADD operation', () => {
    const item = diagnostics({
      operations: [
        operation('UNPACK_EXEC_CREATE_DIRECTORY', 'APPLIED', '2026-10-23T05:00:00Z'),
        operation('UNPACK_EXEC_QBITTORRENT_ADD', 'APPLIED', '2026-10-23T05:00:02Z'),
      ],
    });
    const steps = unpackExecutionDetailTimeline(item);
    expect(steps.map((x) => x.label)).toContain('向下载器添加种子');
    expect(steps.map((x) => x.label)).not.toContain('请求下载器校验');
    expect(steps.map((x) => x.label)).not.toContain('请求开始下载或做种');
  });
  it('never marks an unconfirmed operation as successful', () => {
    const steps = unpackExecutionDetailTimeline(
      diagnostics({
        operations: [operation('UNPACK_EXEC_QBITTORRENT_ADD', 'INTENT_RECORDED')],
        requires_manual_reconciliation: true,
      }),
    );
    expect(steps[0]?.result).toBe('attention');
    expect(steps[0]?.note).toContain('实际结果尚未确认');
    expect(
      unpackExecutionDetailSummary(diagnostics({ requires_manual_reconciliation: true }))
        .recommendationLabel,
    ).toBe('先人工核对');
  });
  it('handles unknown stage and empty operation journal without inventing successes', () => {
    const steps = unpackExecutionDetailTimeline(
      diagnostics({
        status: 'MATCH_ERROR',
        last_error_code: null,
        last_error_message: null,
        execution_stage: null,
        operations: [],
      }),
    );
    expect(steps).toEqual([]);
  });
  it('translates the skip-checking failure to a readable but cautious explanation', () => {
    const summary = unpackExecutionDetailSummary(
      diagnostics({
        last_error_code: 'UNPACK_SEED_SKIP_CHECK_NOT_COMPLETE',
        last_error_message: 'skip checking',
      }),
    );
    expect(summary.title).toContain('下载器');
    expect(summary.description).toContain('停止了后续操作');
    expect(summary.recommendation).toContain('只读查询');
    expect(summary.description).not.toContain('skip checking');
  });
  it('summarizes record states for history table', () => {
    expect(unpackExecutionDetailRecordNote(operation('UNPACK_EXEC_MATERIALIZE_HARDLINK'))).toBe(
      '文件',
    );
  });
});
