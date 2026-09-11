import { describe, expect, it } from 'vitest';

import {
  cancellationIsInProgress,
  cancellationOptionsAreConsistent,
  canStartTaskCancellation,
  createTaskActionIdempotencyKey,
  formatByteUpperBound,
} from './taskActionSafety';

describe('公开任务动作 UI 安全门', () => {
  it('只允许副作用阶段发起新的取消请求', () => {
    expect(canStartTaskCancellation('LINKING')).toBe(true);
    expect(canStartTaskCancellation('ADDING')).toBe(true);
    expect(canStartTaskCancellation('CLIENT_VERIFYING')).toBe(true);
    expect(canStartTaskCancellation('SEEDING')).toBe(true);
    expect(canStartTaskCancellation('AWAITING_CONFIRMATION')).toBe(false);
    expect(canStartTaskCancellation('DONE')).toBe(false);
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
