export interface TaskLifecycleFeedbackItem {
  authorization_status: string;
}

export type TaskLifecycleFeedback =
  | { level: 'info'; message: string }
  | { level: 'warning'; message: string }
  | { level: 'success'; message: string };

export function taskLifecycleAdvanceFeedback(
  items: readonly TaskLifecycleFeedbackItem[],
): TaskLifecycleFeedback {
  const reviewRequired = items.filter(
    (item) => item.authorization_status === 'REVIEW_REQUIRED',
  ).length;
  const approvalRequired = items.filter(
    (item) => item.authorization_status === 'APPROVAL_REQUIRED',
  ).length;

  if (reviewRequired > 0) {
    return {
      level: 'info',
      message: `${reviewRequired} 个对象已完成分析，等待候选确认后可继续推进`,
    };
  }
  if (approvalRequired > 0) {
    return {
      level: 'warning',
      message: `${approvalRequired} 个对象需要高风险授权，当前未执行对应副作用`,
    };
  }
  return {
    level: 'success',
    message: '统一生命周期已推进到当前可安全到达的阶段',
  };
}
