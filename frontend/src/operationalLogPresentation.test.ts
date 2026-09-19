import { describe, expect, it } from 'vitest';

import type { OperationalLogEntry } from './api/system';
import {
  formatOperationalLogFields,
  operationalLoggerLabel,
  systemLogPresentation,
} from './operationalLogPresentation';

function entry(overrides: Partial<OperationalLogEntry>): OperationalLogEntry {
  return {
    timestamp: '2026-09-19T03:00:00Z',
    level: 'INFO',
    source: 'SYSTEM',
    logger: 'packbreaker.http',
    message: '请求处理完成',
    fields: {},
    exception: null,
    event_code: null,
    task_id: null,
    task_name: null,
    execution_id: null,
    trace_id: null,
    ...overrides,
  };
}

describe('operational log presentation', () => {
  it('turns generic HTTP logs into a concrete Chinese action', () => {
    const item = entry({
      fields: {
        method: 'GET',
        path: '/api/v1/notification-channels',
        status_code: 200,
        client_source: '127.0.0.1',
        duration_ms: 3.063,
      },
    });
    expect(systemLogPresentation(item)).toEqual({
      title: '获取通知渠道列表完成',
      detail: 'HTTP 状态码 200',
    });
    expect(formatOperationalLogFields(item)).toContain('请求方式：GET');
    expect(formatOperationalLogFields(item)).toContain('接口：/api/v1/notification-channels');
    expect(formatOperationalLogFields(item)).toContain('耗时：3.1 ms');
  });

  it('translates common Alembic messages', () => {
    expect(
      systemLogPresentation(
        entry({
          logger: 'alembic.runtime.migration',
          message: 'Will assume non-transactional DDL.',
        }),
      ),
    ).toEqual({
      title: '数据库迁移模式已确认',
      detail: 'SQLite DDL 按非事务模式执行',
    });
    expect(operationalLoggerLabel('alembic.runtime.migration')).toBe('数据库迁移');
  });
});
