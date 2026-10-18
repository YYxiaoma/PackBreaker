import { describe, expect, it } from 'vitest';

import {
  NAVIGATION_GROUPS,
  PRIMARY_ROUTE_NAMES,
  SECONDARY_ROUTE_PATHS,
  ensurePrimaryExpanded,
  getDefaultRoute,
  getPrimaryRoute,
  normalizeRoute,
  togglePrimaryExpansion,
} from './navigation';

describe('v1.1.4 navigation', () => {
  it('defines the approved four-level primary navigation', () => {
    expect(PRIMARY_ROUTE_NAMES).toEqual(['总览', '任务中心', '配置中心', '系统管理']);
    expect(NAVIGATION_GROUPS).toEqual([
      { name: '总览', children: [] },
      { name: '任务中心', children: ['数据拆包', '数据去重'] },
      { name: '配置中心', children: ['CookieCloud配置', '站点配置', '下载器配置'] },
      { name: '系统管理', children: ['通知配置', '日志浏览', 'Agent配置', '备份恢复'] },
    ]);
  });

  it('maps primary routes to their approved default child paths', () => {
    expect(normalizeRoute('任务中心')).toBe(SECONDARY_ROUTE_PATHS.数据拆包);
    expect(normalizeRoute('配置中心')).toBe(SECONDARY_ROUTE_PATHS.CookieCloud配置);
    expect(normalizeRoute('系统管理')).toBe(SECONDARY_ROUTE_PATHS.通知配置);
    expect(getDefaultRoute('任务中心')).toBe(SECONDARY_ROUTE_PATHS.数据拆包);
    expect(getPrimaryRoute(SECONDARY_ROUTE_PATHS.数据拆包)).toBe('任务中心');
    expect(getPrimaryRoute(SECONDARY_ROUTE_PATHS.站点配置)).toBe('配置中心');
    expect(getPrimaryRoute(SECONDARY_ROUTE_PATHS.Agent配置)).toBe('系统管理');
  });

  it('accepts every new child label and keeps About outside the primary groups', () => {
    for (const [label, path] of Object.entries(SECONDARY_ROUTE_PATHS)) {
      expect(normalizeRoute(label)).toBe(path);
      expect(normalizeRoute(path)).toBe(path);
    }
    expect(normalizeRoute('关于')).toBe('关于');
    expect(getPrimaryRoute('关于')).toBeNull();
  });

  it('supports multiple primary groups expanded at the same time', () => {
    expect(togglePrimaryExpansion([], '任务中心')).toEqual(['任务中心']);
    expect(togglePrimaryExpansion(['任务中心'], '配置中心')).toEqual(['任务中心', '配置中心']);
    expect(togglePrimaryExpansion(['任务中心', '配置中心'], '系统管理')).toEqual([
      '任务中心',
      '配置中心',
      '系统管理',
    ]);
    expect(togglePrimaryExpansion(['任务中心', '配置中心'], '任务中心')).toEqual(['配置中心']);
  });

  it('preserves other expanded groups when the current route changes to a child', () => {
    expect(ensurePrimaryExpanded(['任务中心'], '配置中心')).toEqual(['任务中心', '配置中心']);
    expect(ensurePrimaryExpanded(['任务中心', '配置中心'], '配置中心')).toEqual([
      '任务中心',
      '配置中心',
    ]);
    expect(ensurePrimaryExpanded(['任务中心'], null)).toEqual([]);
  });

  it('migrates legacy routes without restoring removed primary entries', () => {
    expect(normalizeRoute('站点管理')).toBe(SECONDARY_ROUTE_PATHS.站点配置);
    expect(normalizeRoute('系统设置')).toBe(SECONDARY_ROUTE_PATHS.通知配置);
    expect(normalizeRoute('下载器')).toBe(SECONDARY_ROUTE_PATHS.下载器配置);
    expect(normalizeRoute('日志')).toBe(SECONDARY_ROUTE_PATHS.日志浏览);
    expect(normalizeRoute('AI助手')).toBe(SECONDARY_ROUTE_PATHS.Agent配置);
    expect(normalizeRoute('AI 助手')).toBe(SECONDARY_ROUTE_PATHS.Agent配置);
    expect(normalizeRoute('CookieCloud同步')).toBe(SECONDARY_ROUTE_PATHS.CookieCloud配置);
    expect(normalizeRoute('unknown')).toBe('总览');
    expect(PRIMARY_ROUTE_NAMES).not.toContain('站点管理' as never);
    expect(PRIMARY_ROUTE_NAMES).not.toContain('系统设置' as never);
  });
});
