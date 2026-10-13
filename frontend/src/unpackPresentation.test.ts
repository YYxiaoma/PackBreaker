import { describe, expect, it } from 'vitest';

import {
  unpackDefinitionStatusLabel,
  unpackExecutionStatusLabel,
  unpackItemExternalRiskLabel,
  unpackReconciliationGuidance,
  unpackItemStatusLabel,
  unpackItemStatusSummary,
  unpackPlanStatusLabel,
  unpackPlanBlockReasonLabel,
  unpackProgressPercent,
  unpackSourceKindLabel,
  unpackTriggerLabel,
} from './unpackPresentation';

describe('unpack presentation', () => {
  it('translates every user-visible definition status', () => {
    expect(unpackDefinitionStatusLabel('PENDING_EXECUTION')).toBe('待执行');
    expect(unpackDefinitionStatusLabel('ENABLED')).toBe('已启用');
    expect(unpackDefinitionStatusLabel('PAUSED')).toBe('已暂停');
    expect(unpackDefinitionStatusLabel('ERROR')).toBe('异常');
  });

  it('translates execution and item states without exposing enum values', () => {
    expect(unpackExecutionStatusLabel('REVIEW_REQUIRED')).toBe('待人工审核');
    expect(unpackExecutionStatusLabel('CLIENT_VERIFYING')).toBe('下载器校验中');
    expect(unpackItemStatusLabel('MATCHED_AUTO')).toBe('自动匹配成功');
    expect(unpackItemStatusLabel('AUXILIARY_FETCHING')).toBe('补齐辅助文件');
    expect(unpackItemStatusLabel('EXECUTION_ERROR')).toBe('执行错误');
  });

  it('does not misrepresent blocked frozen plans as ready or live-verified', () => {
    expect(unpackItemStatusLabel('PLAN_PENDING')).toBe('执行计划待处理');
    expect(
      unpackItemStatusSummary({
        status: 'PLAN_PENDING',
        last_error_code: 'UNPACK_EXECUTION_PLAN_BLOCKED',
      }),
    ).toBe('执行计划已阻断');
    expect(unpackItemStatusSummary({ status: 'PLAN_PENDING', last_error_code: null })).toBe(
      '执行计划待处理',
    );
    expect(unpackPlanStatusLabel('BLOCKED')).toContain('阻断');
    expect(unpackPlanStatusLabel('READY')).toContain('未实时复核');
    expect(unpackPlanStatusLabel('INVALID')).toContain('无效');
    expect(unpackPlanBlockReasonLabel('TARGET_PARENT_UNSAFE')).toContain('目标目录');
    expect(unpackPlanBlockReasonLabel('untrusted-raw-text')).toBe('其他安全阻断');
  });

  it('exposes post-START drift as manual reconciliation instead of normal retry', () => {
    const atRisk = {
      status: 'EXECUTION_ERROR',
      has_external_operations: true,
      last_error_code: 'UNPACK_EXEC_MATERIALIZATION_TARGET_DRIFT',
    } as const;
    expect(unpackItemExternalRiskLabel(atRisk)).toContain('可能已启动');
    expect(unpackItemExternalRiskLabel(atRisk)).toContain('人工对账');
    expect(
      unpackItemExternalRiskLabel({
        ...atRisk,
        last_error_code: 'UNPACK_EXEC_MATERIALIZATION_PROOF_MISSING',
      }),
    ).toContain('人工对账');
    expect(
      unpackItemExternalRiskLabel({ ...atRisk, last_error_code: 'UNPACK_SEED_RECONCILE_REQUIRED' }),
    ).toContain('人工对账');
    expect(unpackItemExternalRiskLabel({ ...atRisk, has_external_operations: false })).toBeNull();
    expect(unpackItemExternalRiskLabel({ ...atRisk, status: 'MATCH_ERROR' })).toBeNull();
    expect(unpackItemExternalRiskLabel({ ...atRisk, last_error_code: 'SITE_TIMEOUT' })).toBeNull();
  });

  it('classifies unresolved client writes without suggesting an unsafe retry', () => {
    const historical = {
      reconciliation_reasons: ['EXTERNAL_WRITE_UNCONFIRMED'],
      requires_manual_reconciliation: true,
      operations_truncated: false,
    };
    const steps = unpackReconciliationGuidance(historical, {
      status: 'UNRESOLVED_ADD',
      error_code: 'ADD_RESULT_UNCONFIRMED',
    });
    expect(steps.join(' ')).toContain('请求是否到达下载器');
    expect(steps.join(' ')).toContain('不能自动重试');
  });

  it('does not clear a historical journal conflict after a live ownership match', () => {
    const steps = unpackReconciliationGuidance(
      {
        reconciliation_reasons: ['JOURNAL_RECONCILE_REQUIRED'],
        requires_manual_reconciliation: true,
        operations_truncated: true,
      },
      { status: 'MATCHED', error_code: null },
    );
    expect(steps.join(' ')).toContain('不代表对账完成');
    expect(steps.join(' ')).toContain('截断');
    expect(steps.join(' ')).toContain('人工核对');
  });

  it('calls out conflicting qB and Transmission ADD proofs without claiming a safe client', () => {
    const steps = unpackReconciliationGuidance(
      {
        reconciliation_reasons: ['MULTIPLE_DOWNLOADER_ADD_PROOFS'],
        requires_manual_reconciliation: true,
        operations_truncated: false,
      },
      { status: 'UNKNOWN', error_code: 'ADD_PROOF_AMBIGUOUS' },
    );
    expect(steps.join(' ')).toContain('qB');
    expect(steps.join(' ')).toContain('Transmission');
    expect(steps.join(' ')).toContain('不能自动选择');
    expect(steps.join(' ')).toContain('人工核对');
  });

  it('distinguishes missing, conflict and unknown downloader observations', () => {
    const base = {
      reconciliation_reasons: [],
      requires_manual_reconciliation: false,
      operations_truncated: false,
    };
    expect(unpackReconciliationGuidance(base, { status: 'MISSING' }).join(' ')).toContain('未找到');
    expect(
      unpackReconciliationGuidance(base, { status: 'OWNERSHIP_MISMATCH' }).join(' '),
    ).toContain('归属');
    expect(unpackReconciliationGuidance(base, { status: 'UNKNOWN' }).join(' ')).toContain('未知');
    expect(unpackReconciliationGuidance(base, null).join(' ')).toContain('未执行');
  });

  it('translates task kind/source and clamps progress', () => {
    expect(unpackTriggerLabel('MANUAL')).toBe('手动拆包');
    expect(unpackTriggerLabel('MONITOR')).toBe('监控拆包');
    expect(unpackSourceKindLabel('DIRECTORY')).toBe('目录');
    expect(unpackSourceKindLabel('DOWNLOADER')).toBe('下载器');
    expect(unpackProgressPercent(10, 3)).toBe(30);
    expect(unpackProgressPercent(0, 0)).toBe(0);
  });
});
