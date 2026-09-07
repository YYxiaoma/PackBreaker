// 合成演示模型，不是服务端 API 类型或生产安全实现。
export type Level = 'FULL_VERIFIED' | 'CLIENT_CHECK_REQUIRED' | 'BLOCKED';
export type State =
  | 'VERIFYING'
  | 'SEARCHING'
  | 'AWAITING_CONFIRMATION'
  | 'SEEDING'
  | 'FAILED'
  | 'PAUSED'
  | 'CANCELLED'
  | 'CLIENT_VERIFYING'
  | 'DONE';
export interface Task {
  id: string;
  name: string;
  size: string;
  site: string;
  client: string;
  state: State;
  level: Level;
  progress: number;
  kind: string;
  units: number;
  error?: string;
  previous?: State;
  events: string[];
}
export const stateNames: Record<State, string> = {
  VERIFYING: '完整校验中',
  SEARCHING: '站点搜索中',
  AWAITING_CONFIRMATION: '待人工确认',
  SEEDING: '正在做种',
  FAILED: '处理失败',
  PAUSED: '已暂停',
  CANCELLED: '已取消',
  CLIENT_VERIFYING: '下载器校验中',
  DONE: '已完成',
};
export const levelNames: Record<Level, string> = {
  FULL_VERIFIED: '已完整验证',
  CLIENT_CHECK_REQUIRED: '需下载器校验',
  BLOCKED: '安全阻断',
};
export function seedTasks(): Task[] {
  const rows: [string, string, string, State, Level, number, string, number][] = [
    [
      '科幻电影合集 · Vol.01',
      '3.30 TB',
      'M-Team',
      'VERIFYING',
      'CLIENT_CHECK_REQUIRED',
      72,
      '电影大包',
      24,
    ],
    [
      '深空纪事 · 第一季',
      '86.4 GB',
      'HDTime',
      'AWAITING_CONFIRMATION',
      'FULL_VERIFIED',
      100,
      '剧集拆包',
      8,
    ],
    [
      '城市档案 · 纪录片合集',
      '248 GB',
      'M-Team',
      'SEARCHING',
      'CLIENT_CHECK_REQUIRED',
      36,
      '电影大包',
      12,
    ],
    ['远山回声 · 2024', '48.6 GB', 'HDTime', 'AWAITING_CONFIRMATION', 'BLOCKED', 0, '历史影片', 1],
    ['海岸来信 · 2023', '21.8 GB', 'M-Team', 'SEEDING', 'FULL_VERIFIED', 100, '电影大包', 1],
    ['明日航线 · 2025', '38.2 GB', 'HDTime', 'SEEDING', 'FULL_VERIFIED', 100, '电影大包', 1],
    [
      '森林之境 · 第二季',
      '62.7 GB',
      'HDTime',
      'AWAITING_CONFIRMATION',
      'CLIENT_CHECK_REQUIRED',
      99,
      '剧集拆包',
      6,
    ],
    ['极地观察 · 合集', '120 GB', 'M-Team', 'FAILED', 'BLOCKED', 42, '电影大包', 4],
    ['蓝色星球 · 特别篇', '18.6 GB', 'M-Team', 'DONE', 'FULL_VERIFIED', 100, '历史影片', 1],
    ['光影之间 · 2022', '32.1 GB', 'HDTime', 'PAUSED', 'CLIENT_CHECK_REQUIRED', 28, '历史影片', 1],
  ];
  return rows.map((r, i) => ({
    id: `PB-${248 - i}`,
    name: r[0],
    size: r[1],
    site: r[2],
    state: r[3],
    level: r[4],
    progress: r[5],
    kind: r[6],
    units: r[7],
    client: [2, 4, 6].includes(i) ? 'Transmission' : 'qBittorrent',
    error: i === 3 ? 'FILE_MAPPING_AMBIGUOUS' : i === 7 ? 'CROSS_DEVICE_LINK' : undefined,
    events: ['14:08 源任务稳定期检查通过（演示）', '14:09 已识别处理单元，查询候选（演示）'],
  }));
}
export function canApprove(task: Task): boolean {
  return task.state === 'AWAITING_CONFIRMATION' && task.level !== 'BLOCKED';
}
export function maySkipVerification(task: Task, enabled: boolean): boolean {
  return enabled && task.client === 'qBittorrent' && task.level === 'FULL_VERIFIED';
}
export function approveTask(task: Task): boolean {
  if (!canApprove(task)) return false;
  task.state = 'CLIENT_VERIFYING';
  task.progress = 0;
  task.events.push('已批准预演，模拟暂停添加并等待下载器完整校验');
  return true;
}
export function finishVerification(task: Task): boolean {
  if (task.state !== 'CLIENT_VERIFYING') return false;
  task.state = 'SEEDING';
  task.progress = 100;
  task.events.push('演示下载器返回 100%，模拟确认做种');
  return true;
}
export function pauseTask(task: Task): boolean {
  if (!['VERIFYING', 'SEARCHING', 'CLIENT_VERIFYING'].includes(task.state)) return false;
  task.previous = task.state;
  task.state = 'PAUSED';
  task.events.push('已暂停演示任务');
  return true;
}
export function resumeTask(task: Task): boolean {
  if (task.state !== 'PAUSED') return false;
  task.state = task.previous ?? 'SEARCHING';
  task.events.push('从演示检查点恢复');
  return true;
}
export function filterTasks(
  tasks: Task[],
  tab: string,
  query: string,
  site: string,
  client: string,
): Task[] {
  return tasks.filter(
    (t) =>
      (tab === '全部任务' ||
        (tab === '进行中' && ['VERIFYING', 'SEARCHING', 'CLIENT_VERIFYING'].includes(t.state)) ||
        (tab === '待确认' && t.state === 'AWAITING_CONFIRMATION') ||
        (tab === '已完成' && ['DONE', 'SEEDING'].includes(t.state)) ||
        (tab === '异常 / 暂停' && ['FAILED', 'PAUSED', 'CANCELLED'].includes(t.state))) &&
      (!query || `${t.name} ${t.id}`.toLowerCase().includes(query.toLowerCase())) &&
      (!site || t.site === site) &&
      (!client || t.client === client),
  );
}
