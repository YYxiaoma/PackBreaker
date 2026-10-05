import type { OperationalLogEntry } from './api/system';

export interface OperationalLogPresentation {
  title: string;
  detail?: string;
}

const LOGGER_LABELS: Record<string, string> = {
  'packbreaker.http': 'HTTP 请求',
  'packbreaker.recovery': '任务恢复',
  'packbreaker.site_reliability': '站点可靠性',
  'packbreaker.task_execution': '任务执行',
  'alembic.runtime.migration': '数据库迁移',
  'uvicorn.error': '应用服务',
};

const FIELD_LABELS: Record<string, string> = {
  trace_id: 'Trace ID',
  method: '请求方式',
  path: '接口',
  status_code: '状态码',
  client_source: '客户端来源',
  duration_ms: '耗时',
  task_id: '任务 ID',
  task_definition_id: '任务定义 ID',
  execution_id: '执行记录 ID',
  event_code: '事件代码',
  request_id: '请求 ID',
  error_code: '错误代码',
  downloader_id: '下载器 ID',
  site_id: '站点 ID',
  source_kind: '来源类型',
  source_object_key: '来源对象',
  technical_detail: '技术详情',
  items: '对象总数',
  ready: '可执行',
  failed: '失败',
  skipped: '跳过',
  discovered: '扫描发现',
  new: '新增对象',
  risk_level: '风险等级',
  blocked_reasons: '阻断原因',
  execution_item_id: '执行对象 ID',
  unpack_task_id: '安全 Run ID',
  source_execution_id: '原执行记录 ID',
  execution_plan_id: '执行计划 ID',
  approval_id: '审批 ID',
  from_phase: '原阶段',
  to_phase: '新阶段',
  from_result: '原结果',
  to_result: '新结果',
  trigger: '触发方式',
};

const EXACT_RESOURCE_LABELS: Record<string, string> = {
  '/': '管理页面',
  '/api/v1/notification-channels': '通知渠道列表',
  '/api/v1/notifications/inbox/unread-count': '站内通知未读数量',
  '/api/v1/system/health': '系统健康状态',
  '/api/v1/system/logs': '运行日志列表',
  '/api/v1/downloaders': '下载器列表',
  '/api/v1/sites': '站点列表',
  '/api/v1/task-definitions': '任务定义列表',
};

const RESOURCE_PREFIXES: ReadonlyArray<readonly [string, string]> = [
  ['/api/v1/notifications/inbox', '站内通知'],
  ['/api/v1/notification-channels', '通知渠道'],
  ['/api/v1/system/health', '系统健康状态'],
  ['/api/v1/system/logs/export', '运行日志导出'],
  ['/api/v1/system/logs', '运行日志'],
  ['/api/v1/system/backups', '备份配置'],
  ['/api/v1/system/upgrade', '系统升级'],
  ['/api/v1/health/live', '服务存活状态'],
  ['/api/v1/health/ready', '服务就绪状态'],
  ['/api/v1/task-definitions', '任务定义'],
  ['/api/v1/tasks', '执行引擎任务'],
  ['/api/v1/downloaders', '下载器'],
  ['/api/v1/sites', '站点'],
  ['/api/v1/ai-agent', 'AI 助手'],
  ['/api/v1/auth/me', '管理员会话状态'],
  ['/api/v1/auth/login', '管理员登录'],
  ['/api/v1/auth/logout', '管理员退出'],
  ['/api/v1/auth/password', '管理员密码'],
  ['/assets', '前端静态资源'],
];

function stringField(entry: OperationalLogEntry, key: string): string | undefined {
  const value = entry.fields[key];
  if (typeof value === 'string') return value;
  if (typeof value === 'number') return String(value);
  return undefined;
}

function numberField(entry: OperationalLogEntry, key: string): number | undefined {
  const value = entry.fields[key];
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return undefined;
}

function resourceLabel(path: string): string {
  const exact = EXACT_RESOURCE_LABELS[path];
  if (exact) return exact;
  const match = RESOURCE_PREFIXES.find(
    ([prefix]) => path === prefix || path.startsWith(`${prefix}/`),
  );
  return match?.[1] ?? `接口 ${path}`;
}

function requestAction(method: string, path: string, resource: string): string {
  if (method === 'GET') return `获取${resource}`;
  if (method === 'DELETE') return `删除${resource}`;
  if (method === 'PUT' || method === 'PATCH') return `更新${resource}`;
  if (method === 'POST' && path.endsWith('/test')) return `测试${resource}连接`;
  if (method === 'POST' && path.includes('/actions')) return `执行${resource}操作`;
  if (method === 'POST') return `提交${resource}操作`;
  return `${method} ${resource}`;
}

function httpPresentation(entry: OperationalLogEntry): OperationalLogPresentation | undefined {
  if (entry.logger !== 'packbreaker.http') return undefined;
  const method = stringField(entry, 'method') ?? 'HTTP';
  const path = stringField(entry, 'path');
  const status = numberField(entry, 'status_code');
  if (!path) return { title: entry.message };
  const resource = resourceLabel(path);
  const action = requestAction(method, path, resource);
  const outcome = status !== undefined && status >= 400 ? '失败' : '完成';
  return {
    title: `${action}${outcome}`,
    detail: status !== undefined ? `HTTP 状态码 ${status}` : undefined,
  };
}

function migrationPresentation(message: string): OperationalLogPresentation | undefined {
  if (message === 'Context impl SQLiteImpl.') {
    return { title: '数据库迁移引擎已初始化', detail: '当前数据库：SQLite' };
  }
  if (message === 'Will assume non-transactional DDL.') {
    return { title: '数据库迁移模式已确认', detail: 'SQLite DDL 按非事务模式执行' };
  }
  const upgrade = /^Running upgrade (.*?) -> ([^,]+)(?:, (.*))?$/.exec(message);
  if (upgrade) {
    const from = upgrade[1]?.trim() || '初始版本';
    const to = upgrade[2]?.trim() || '未知版本';
    return {
      title: `正在执行数据库升级：${from} → ${to}`,
      detail: upgrade[3]?.trim() || undefined,
    };
  }
  const downgrade = /^Running downgrade (.*?) -> ([^,]+)(?:, (.*))?$/.exec(message);
  if (downgrade) {
    const from = downgrade[1]?.trim() || '当前版本';
    const to = downgrade[2]?.trim() || '未知版本';
    return {
      title: `正在执行数据库降级：${from} → ${to}`,
      detail: downgrade[3]?.trim() || undefined,
    };
  }
  return undefined;
}

function servicePresentation(message: string): OperationalLogPresentation | undefined {
  if (/^Started server process/.test(message)) return { title: 'Web 服务进程已启动' };
  if (message === 'Waiting for application startup.') return { title: '正在等待应用初始化' };
  if (message === 'Application startup complete.') return { title: '应用初始化完成' };
  if (/^Uvicorn running on /.test(message)) return { title: 'Web 服务已开始监听请求' };
  if (message === 'Shutting down') return { title: 'Web 服务正在停止' };
  if (message === 'Application shutdown complete.') return { title: '应用已安全停止' };
  return undefined;
}

export function systemLogPresentation(entry: OperationalLogEntry): OperationalLogPresentation {
  const http = httpPresentation(entry);
  if (http) return http;
  if (entry.logger === 'alembic.runtime.migration') {
    return migrationPresentation(entry.message) ?? { title: entry.message };
  }
  if (entry.logger === 'uvicorn.error') {
    return servicePresentation(entry.message) ?? { title: entry.message };
  }
  return { title: entry.message };
}

export function operationalLoggerLabel(logger: string): string {
  return LOGGER_LABELS[logger] ?? logger;
}

function fieldValue(key: string, value: unknown): string {
  if (key === 'duration_ms' && typeof value === 'number') return `${value.toFixed(1)} ms`;
  if (Array.isArray(value)) return value.map((item) => String(item)).join('、');
  if (typeof value === 'string') {
    const labels: Record<string, Record<string, string>> = {
      source_kind: { DIRECTORY: '目录', DOWNLOADER: '下载器' },
      risk_level: { UNKNOWN: '待评估', LOW: '低风险', HIGH: '高风险' },
      trigger: {
        MANUAL: '手动执行',
        CRON: 'Cron 调度',
        IMMEDIATE_SCAN: '立即扫描',
        FAILED_RETRY: '失败重试',
        SYSTEM_RECOVERY: '系统恢复',
      },
      from_phase: {
        WAITING: '等待执行',
        DISCOVERING: '发现源数据',
        ANALYZING: '分析文件',
        SCANNING_SITE: '扫描站点',
        PREPARING: '准备执行',
        UNPACKING: '正在拆包',
        OUTPUTTING: '写入输出',
        VERIFYING: '验证结果',
        COMPLETED: '已完成',
        FAILED: '失败',
        SKIPPED: '已跳过',
      },
      to_phase: {
        WAITING: '等待执行',
        DISCOVERING: '发现源数据',
        ANALYZING: '分析文件',
        SCANNING_SITE: '扫描站点',
        PREPARING: '准备执行',
        UNPACKING: '正在拆包',
        OUTPUTTING: '写入输出',
        VERIFYING: '验证结果',
        COMPLETED: '已完成',
        FAILED: '失败',
        SKIPPED: '已跳过',
      },
      from_result: {
        SUCCESS: '成功',
        FAILED: '失败',
        SKIPPED: '已跳过',
        CANCELLED: '已取消',
      },
      to_result: {
        SUCCESS: '成功',
        FAILED: '失败',
        SKIPPED: '已跳过',
        CANCELLED: '已取消',
      },
    };
    return labels[key]?.[value] ?? value;
  }
  return JSON.stringify(value);
}

export function formatOperationalLogFields(entry: OperationalLogEntry): string {
  return Object.entries(entry.fields)
    .filter(
      ([key]) =>
        entry.source !== 'TASK_EVENT' ||
        !['event_code', 'trace_id', 'execution_id', 'task_id'].includes(key),
    )
    .slice(0, 12)
    .map(([key, value]) => `${FIELD_LABELS[key] ?? key}：${fieldValue(key, value)}`)
    .join(' · ');
}
