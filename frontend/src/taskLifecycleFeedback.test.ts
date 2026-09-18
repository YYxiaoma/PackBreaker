import { describe, expect, it } from 'vitest';

import { taskLifecycleAdvanceFeedback } from './taskLifecycleFeedback';

describe('统一生命周期推进提示', () => {
  it('候选审核优先提示等待 Review', () => {
    expect(
      taskLifecycleAdvanceFeedback([
        { authorization_status: 'APPROVAL_REQUIRED' },
        { authorization_status: 'REVIEW_REQUIRED' },
      ]),
    ).toEqual({
      level: 'info',
      message: '1 个对象已完成分析，等待候选确认后可继续推进',
    });
  });

  it('只有真实待审批对象时才提示高风险授权', () => {
    expect(
      taskLifecycleAdvanceFeedback([
        { authorization_status: 'APPROVAL_REQUIRED' },
        { authorization_status: 'AUTHORIZED' },
      ]),
    ).toEqual({
      level: 'warning',
      message: '1 个对象需要高风险授权，当前未执行对应副作用',
    });
  });

  it('没有 Review 或 Approval 阻断时不会误报 0 个高风险对象', () => {
    expect(
      taskLifecycleAdvanceFeedback([
        { authorization_status: 'AUTHORIZED' },
        { authorization_status: 'COMPLETE' },
      ]),
    ).toEqual({
      level: 'success',
      message: '统一生命周期已推进到当前可安全到达的阶段',
    });
  });
});
