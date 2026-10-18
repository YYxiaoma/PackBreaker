export const PRIMARY_ROUTE_NAMES = ['总览', '任务中心', '配置中心', '系统管理'] as const;

export type PrimaryRoute = (typeof PRIMARY_ROUTE_NAMES)[number];

export const SECONDARY_ROUTE_NAMES = [
  '数据拆包',
  '数据去重',
  'CookieCloud配置',
  '站点配置',
  '下载器配置',
  '通知配置',
  '日志浏览',
  'Agent配置',
  '备份恢复',
] as const;

export type SecondaryRoute = (typeof SECONDARY_ROUTE_NAMES)[number];

export const SECONDARY_ROUTE_PATHS = {
  数据拆包: '任务中心/数据拆包',
  数据去重: '任务中心/数据去重',
  CookieCloud配置: '配置中心/CookieCloud配置',
  站点配置: '配置中心/站点配置',
  下载器配置: '配置中心/下载器配置',
  通知配置: '系统管理/通知配置',
  日志浏览: '系统管理/日志浏览',
  Agent配置: '系统管理/Agent配置',
  备份恢复: '系统管理/备份恢复',
} as const;

export type SecondaryRoutePath = (typeof SECONDARY_ROUTE_PATHS)[SecondaryRoute];

export type Route = PrimaryRoute | SecondaryRoutePath | '关于';

export const DEFAULT_ROUTE_BY_PRIMARY: Record<Exclude<PrimaryRoute, '总览'>, SecondaryRoutePath> = {
  任务中心: SECONDARY_ROUTE_PATHS.数据拆包,
  配置中心: SECONDARY_ROUTE_PATHS.CookieCloud配置,
  系统管理: SECONDARY_ROUTE_PATHS.通知配置,
};

export const NAVIGATION_GROUPS = [
  { name: '总览', children: [] },
  { name: '任务中心', children: ['数据拆包', '数据去重'] },
  { name: '配置中心', children: ['CookieCloud配置', '站点配置', '下载器配置'] },
  { name: '系统管理', children: ['通知配置', '日志浏览', 'Agent配置', '备份恢复'] },
] as const;

const ROUTE_PARENT: Record<SecondaryRoutePath, PrimaryRoute> = {
  [SECONDARY_ROUTE_PATHS.数据拆包]: '任务中心',
  [SECONDARY_ROUTE_PATHS.数据去重]: '任务中心',
  [SECONDARY_ROUTE_PATHS.CookieCloud配置]: '配置中心',
  [SECONDARY_ROUTE_PATHS.站点配置]: '配置中心',
  [SECONDARY_ROUTE_PATHS.下载器配置]: '配置中心',
  [SECONDARY_ROUTE_PATHS.通知配置]: '系统管理',
  [SECONDARY_ROUTE_PATHS.日志浏览]: '系统管理',
  [SECONDARY_ROUTE_PATHS.Agent配置]: '系统管理',
  [SECONDARY_ROUTE_PATHS.备份恢复]: '系统管理',
};

const LEGACY_ROUTE_ALIASES: Record<string, Route> = {
  站点管理: SECONDARY_ROUTE_PATHS.站点配置,
  系统设置: SECONDARY_ROUTE_PATHS.通知配置,
  下载器: SECONDARY_ROUTE_PATHS.下载器配置,
  日志: SECONDARY_ROUTE_PATHS.日志浏览,
  通知: SECONDARY_ROUTE_PATHS.通知配置,
  AI助手: SECONDARY_ROUTE_PATHS.Agent配置,
  'AI 助手': SECONDARY_ROUTE_PATHS.Agent配置,
  CookieCloud: SECONDARY_ROUTE_PATHS.CookieCloud配置,
  CookieCloud同步: SECONDARY_ROUTE_PATHS.CookieCloud配置,
  站点配置: SECONDARY_ROUTE_PATHS.站点配置,
  配置中心: SECONDARY_ROUTE_PATHS.CookieCloud配置,
  系统管理: SECONDARY_ROUTE_PATHS.通知配置,
  任务中心: SECONDARY_ROUTE_PATHS.数据拆包,
};

function isSecondaryRoutePath(value: string): value is SecondaryRoutePath {
  return Object.values(SECONDARY_ROUTE_PATHS).includes(value as SecondaryRoutePath);
}

function secondaryRoutePath(value: SecondaryRoute): SecondaryRoutePath {
  return SECONDARY_ROUTE_PATHS[value];
}

export function normalizeRoute(value: string): Route {
  const aliased = LEGACY_ROUTE_ALIASES[value] ?? value;

  if (aliased === '关于') return '关于';
  if (isSecondaryRoutePath(aliased)) return aliased;
  if (SECONDARY_ROUTE_NAMES.includes(aliased as SecondaryRoute)) {
    return secondaryRoutePath(aliased as SecondaryRoute);
  }

  if (PRIMARY_ROUTE_NAMES.includes(aliased as PrimaryRoute)) {
    const primary = aliased as PrimaryRoute;
    return primary === '总览' ? primary : DEFAULT_ROUTE_BY_PRIMARY[primary];
  }

  return '总览';
}

export function getPrimaryRoute(route: Route): PrimaryRoute | null {
  if (route === '关于') return null;
  if (PRIMARY_ROUTE_NAMES.includes(route as PrimaryRoute)) return route as PrimaryRoute;
  return ROUTE_PARENT[route as SecondaryRoutePath] ?? null;
}

export function getDefaultRoute(primary: PrimaryRoute): Route {
  if (primary === '总览') return '总览';
  return DEFAULT_ROUTE_BY_PRIMARY[primary];
}

export function togglePrimaryExpansion(
  expanded: readonly PrimaryRoute[],
  primary: PrimaryRoute,
): PrimaryRoute[] {
  return expanded.includes(primary)
    ? expanded.filter((item) => item !== primary)
    : [...expanded, primary];
}

export function ensurePrimaryExpanded(
  expanded: readonly PrimaryRoute[],
  primary: PrimaryRoute | null,
): PrimaryRoute[] {
  if (!primary) return [];
  return expanded.includes(primary) ? [...expanded] : [...expanded, primary];
}

export function getSecondaryRoutePath(route: SecondaryRoute): SecondaryRoutePath {
  return secondaryRoutePath(route);
}

export function normalizePrimaryRoute(value: string): Route {
  return normalizeRoute(value);
}
