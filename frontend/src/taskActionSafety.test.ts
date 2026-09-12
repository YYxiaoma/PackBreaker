import { describe, expect, it } from 'vitest';

import {
  canCancelBeforeSideEffects,
  cancellationIsCooperativeAnalysis,
  cancellationIsInProgress,
  cancellationOptionsAreConsistent,
  cancellationRequiresResourceScope,
  canStartTaskCancellation,
  createTaskActionIdempotencyKey,
  formatByteUpperBound,
} from './taskActionSafety';

describe('公开任务动作 UI 安全门', () => {
  it('稳定零副作用、协作式分析与副作用阶段使用不同取消边界', () => {
    expect(canStartTaskCancellation('PENDING')).toBe(true);
    expect(canStartTaskCancellation('PREFLIGHT')).toBe(true);
    expect(canStartTaskCancellation('AWAITING_CONFIRMATION')).toBe(true);
    expect(canStartTaskCancellation('PAUSED')).toBe(true);
    expect(canStartTaskCancellation('RETRY')).toBe(true);
    expect(canStartTaskCancellation('LINKING')).toBe(true);
    expect(canStartTaskCancellation('ADDING')).toBe(true);
    expect(canStartTaskCancellation('CLIENT_VERIFYING')).toBe(true);
    expect(canStartTaskCancellation('SEEDING')).toBe(true);
    expect(canStartTaskCancellation('ANALYZING')).toBe(true);
    expect(canStartTaskCancellation('SEARCHING')).toBe(true);
    expect(canStartTaskCancellation('MATCHING')).toBe(true);
    expect(canStartTaskCancellation('VERIFYING')).toBe(true);
    expect(canStartTaskCancellation('DONE')).toBe(false);
    expect(canCancelBeforeSideEffects('PENDING')).toBe(true);
    expect(canCancelBeforeSideEffects('ANALYZING')).toBe(true);
    expect(canCancelBeforeSideEffects('AWAITING_CONFIRMATION')).toBe(true);
    expect(canCancelBeforeSideEffects('LINKING')).toBe(false);
    expect(cancellationIsCooperativeAnalysis('ANALYZING')).toBe(true);
    expect(cancellationIsCooperativeAnalysis('VERIFYING')).toBe(true);
    expect(cancellationIsCooperativeAnalysis('PREFLIGHT')).toBe(false);
    expect(cancellationRequiresResourceScope('LINKING')).toBe(true);
    expect(cancellationRequiresResourceScope('SEEDING')).toBe(true);
    expect(cancellationRequiresResourceScope('PREFLIGHT')).toBe(false);
    expect(cancellationIsInProgress('CANCELLING')).toBe(true);
    expect(cancellationIsInProgress('ROLLING_BACK')).toBe(true);
  });

  it('回滚 journal-owned 文件时要求同时移除下载器任务', () => {
    expect(cancellationOptionsAreConsistent(false, false)).toBe(true);
    expect(cancellationOptionsAreConsistent(true, false)).toBe(true);
    expect(cancellationOptionsAreConsistent(true, true)).toBe(true);
    expect(cancellationOptionsAreConsistent(false, true)).toBe(false);
  });

  it('幂等键绑定动作与任务且可在重试期间稳定保存', () => {
    expect(createTaskActionIdempotencyKey('execute', 'task-1', () => 'uuid-1')).toBe(
      'ui-execute-task-1-uuid-1',
    );
    expect(createTaskActionIdempotencyKey('reconcile', 'task-1', () => 'uuid-2')).toBe(
      'ui-reconcile-task-1-uuid-2',
    );
    expect(createTaskActionIdempotencyKey('purge', 'task-1', () => 'uuid-3')).toBe(
      'ui-purge-task-1-uuid-3',
    );
    expect(() => createTaskActionIdempotencyKey('cancel', ' ', () => 'uuid-1')).toThrow(
      'taskId 不能为空',
    );
    expect(() => createTaskActionIdempotencyKey('cancel', 'task-1', () => 'bad uuid')).toThrow(
      '无法生成安全的任务动作幂等键',
    );
  });

  it('以二进制单位展示客户端下载上界', () => {
    expect(formatByteUpperBound(0)).toBe('0 B');
    expect(formatByteUpperBound(1024)).toBe('1.00 KiB');
    expect(formatByteUpperBound(128 * 1024 * 1024)).toBe('128 MiB');
  });
});
