import { describe, expect, it } from 'vitest';
import {
  approveTask,
  canApprove,
  filterTasks,
  finishVerification,
  maySkipVerification,
  pauseTask,
  resumeTask,
  seedTasks,
} from './demo';

describe('演示流程安全门', () => {
  it('映射被阻断时不能批准或进入做种', () => {
    const t = seedTasks()[3]!;
    expect(canApprove(t)).toBe(false);
    expect(approveTask(t)).toBe(false);
    expect(finishVerification(t)).toBe(false);
    expect(t.state).toBe('AWAITING_CONFIRMATION');
  });
  it('只有显式启用、qB、完整验证的组合允许跳过校验', () => {
    const t = seedTasks()[1]!;
    expect(maySkipVerification(t, false)).toBe(false);
    expect(maySkipVerification(t, true)).toBe(true);
    t.level = 'CLIENT_CHECK_REQUIRED';
    expect(maySkipVerification(t, true)).toBe(false);
    t.level = 'FULL_VERIFIED';
    t.client = 'Transmission';
    expect(maySkipVerification(t, true)).toBe(false);
  });
  it('重复批准不重复记录，校验确认后才能做种', () => {
    const t = seedTasks()[1]!;
    const count = t.events.length;
    expect(approveTask(t)).toBe(true);
    expect(t.state).toBe('CLIENT_VERIFYING');
    expect(approveTask(t)).toBe(false);
    expect(t.events.length).toBe(count + 1);
    expect(finishVerification(t)).toBe(true);
    expect(t.state).toBe('SEEDING');
    expect(finishVerification(t)).toBe(false);
  });
  it('需要客户端校验的任务不会因为批准变成完整本地验证', () => {
    const t = seedTasks()[6]!;
    expect(approveTask(t)).toBe(true);
    expect(t.level).toBe('CLIENT_CHECK_REQUIRED');
    expect(t.state).toBe('CLIENT_VERIFYING');
  });
  it('暂停与恢复保留阶段，阻断任务不能绕过审核', () => {
    const t = seedTasks()[0]!;
    expect(pauseTask(t)).toBe(true);
    expect(t.state).toBe('PAUSED');
    expect(resumeTask(t)).toBe(true);
    expect(t.state).toBe('VERIFYING');
    expect(pauseTask(seedTasks()[3]!)).toBe(false);
  });
});
describe('任务筛选', () => {
  it('组合筛选与空状态', () => {
    const rows = seedTasks();
    expect(filterTasks(rows, '待确认', '', 'HDTime', 'qBittorrent')).toHaveLength(2);
    expect(filterTasks(rows, '全部任务', 'pb-248', '', '')).toHaveLength(1);
    expect(filterTasks(rows, '全部任务', '不存在', '', '')).toHaveLength(0);
  });
  it('全部状态计数不因筛选丢失任务', () => {
    const rows = seedTasks();
    const total = ['进行中', '待确认', '已完成', '异常 / 暂停'].reduce(
      (n, s) => n + filterTasks(rows, s, '', '', '').length,
      0,
    );
    expect(total).toBe(rows.length);
  });
});
