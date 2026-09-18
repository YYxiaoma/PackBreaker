export interface TaskEventTranslation {
  title: string;
  description: string;
  translated: boolean;
}

const TASK_EVENT_TRANSLATIONS: Record<string, Omit<TaskEventTranslation, 'translated'>> = {
  TASK_EXECUTION_STARTED: {
    title: '开始执行',
    description: '任务执行已启动，开始读取和分析来源。',
  },
  TASK_EXECUTION_MATERIALIZED: {
    title: '执行已进入安全链',
    description: '符合条件的对象已转换为受控拆包 Run。',
  },
  TASK_EXECUTION_ITEM_STATE_CHANGED: {
    title: '执行对象状态更新',
    description: '底层安全 Run 的阶段或执行结果发生变化。',
  },
  TASK_FAILED_RETRY_STARTED: {
    title: '开始失败对象重试',
    description: '已从原执行中筛选可重试失败对象。',
  },
  TASK_FAILED_OBJECT_RERUN_CREATED: {
    title: '已创建失败对象重试 Run',
    description: '失败对象已创建新的受控重试 Run。',
  },
  TASK_FAILED_OBJECT_RERUN_REJECTED: {
    title: '失败对象重试创建失败',
    description: '失败对象无法创建新的重试 Run。',
  },
  TASK_MONITOR_SCAN_MATERIALIZING: {
    title: '监控扫描发现新对象',
    description: '监控扫描发现符合规则的新对象，准备进入安全执行链。',
  },
  TASK_MONITOR_SCAN_MATERIALIZED: {
    title: '监控扫描已物化',
    description: '本次监控扫描的新对象已转换为受控拆包 Run。',
  },
  TASK_UNPACK_RUN_MATERIALIZED: {
    title: '已创建安全拆包 Run',
    description: '拆包 Run 已创建，等待统一生命周期继续分析、计划与授权。',
  },
  TASK_LIFECYCLE_ANALYZED: {
    title: '分析与预检完成',
    description: '只读分析与 Preflight 已完成，尚未开始真实副作用。',
  },
  TASK_LIFECYCLE_GATE_BLOCKED: {
    title: '执行门阻断',
    description: 'Execution Gate 未满足安全条件，生命周期已在副作用前停止。',
  },
  TASK_LIFECYCLE_PLAN_BLOCKED: {
    title: '执行计划阻断',
    description: 'Execution Plan 当前不可安全执行，生命周期已停止。',
  },
  TASK_LIFECYCLE_APPROVAL_REQUIRED: {
    title: '等待高风险授权',
    description: '计划需要额外授权，当前没有执行对应副作用。',
  },
  TASK_LIFECYCLE_APPROVAL_REJECTED: {
    title: '高风险计划已拒绝',
    description: '当前 Execution Plan 已被拒绝，生命周期保持在真实副作用之前。',
  },
  TASK_LIFECYCLE_PREAUTHORIZED: {
    title: '监控预授权命中',
    description: '当前高风险 action 被监控任务的显式白名单覆盖，并绑定本次 Plan digest。',
  },
  TASK_LIFECYCLE_APPROVED: {
    title: '高风险计划已批准',
    description: '当前 Execution Plan 已获得有效审批证据，可以继续进入既有安全执行器。',
  },
  TASK_APPROVAL_APPROVED: {
    title: 'Web 审批通过',
    description: '管理员通过 Web 批准了当前 Plan；该决定只绑定当前 Plan digest。',
  },
  TASK_APPROVAL_REJECTED: {
    title: 'Web 审批拒绝',
    description: '管理员通过 Web 拒绝了当前 Plan；该 Plan 的决定不能被后续覆盖。',
  },
  TASK_LIFECYCLE_APPROVED_EXECUTION: {
    title: '已审批计划开始执行',
    description: '有效审批后的计划已交给既有 journal-backed 安全执行链。',
  },
  TASK_LIFECYCLE_AUTO_AUTHORIZED: {
    title: '低风险计划已授权',
    description: '当前低风险计划已交给既有 journal-backed 安全执行链。',
  },
  TASK_SOURCE_OBJECT_REJECTED: {
    title: '来源对象处理失败',
    description: '来源对象未能转换为可执行的拆包 Run。',
  },
  TASK_EXECUTION_FAILED: {
    title: '执行失败',
    description: '任务执行在物化或调度阶段失败。',
  },
};

export function taskEventTranslation(eventCode: string | null | undefined): TaskEventTranslation {
  if (!eventCode) {
    return { title: '系统运行日志', description: 'PackBreaker 运行时记录。', translated: true };
  }
  const translation = TASK_EVENT_TRANSLATIONS[eventCode];
  if (!translation) {
    return {
      title: '未翻译事件',
      description: '前端尚未提供此事件代码的中文映射，英文技术信息已完整保留。',
      translated: false,
    };
  }
  return { ...translation, translated: true };
}
