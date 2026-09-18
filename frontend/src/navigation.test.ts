import { describe, expect, it } from 'vitest';

import { PRIMARY_ROUTE_NAMES, normalizePrimaryRoute } from './navigation';

describe('v0.1.8 primary navigation', () => {
  it('removes the legacy preflight and maintenance pages from the primary routes', () => {
    expect(PRIMARY_ROUTE_NAMES).not.toContain('预演与确认');
    expect(PRIMARY_ROUTE_NAMES).not.toContain('清理与对账');
  });

  it('maps removed or unknown hash routes back to the overview', () => {
    expect(normalizePrimaryRoute('预演与确认')).toBe('总览');
    expect(normalizePrimaryRoute('清理与对账')).toBe('总览');
    expect(normalizePrimaryRoute('unknown')).toBe('总览');
    expect(normalizePrimaryRoute('任务中心')).toBe('任务中心');
  });
});
