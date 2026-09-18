export const PRIMARY_ROUTE_NAMES = [
  '总览',
  '任务中心',
  '站点管理',
  '下载器',
  '日志',
  '系统设置',
  '关于',
] as const;

export type PrimaryRoute = (typeof PRIMARY_ROUTE_NAMES)[number];

export function normalizePrimaryRoute(value: string): PrimaryRoute {
  return PRIMARY_ROUTE_NAMES.includes(value as PrimaryRoute) ? (value as PrimaryRoute) : '总览';
}
