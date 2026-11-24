import { describe, expect, it } from 'vitest';

import { siteSyncButtonLabel, siteSyncNeedsReset } from './siteSync';

function state(connectionStatus: string, circuitState: string, id = 'site-1') {
  return {
    site: { id, connection_status: connectionStatus },
    connection: { circuit_state: circuitState },
  };
}

describe('manual site sync mode', () => {
  it('uses a plain sync for a healthy site', () => {
    const healthy = state('OK', 'CLOSED');
    expect(siteSyncNeedsReset(healthy, 'site-1')).toBe(false);
    expect(siteSyncButtonLabel(false, 'CLOSED')).toBe('立即同步');
  });

  it('resets the circuit when the breaker is open', () => {
    const open = state('OK', 'OPEN');
    expect(siteSyncNeedsReset(open, 'site-1')).toBe(true);
    expect(siteSyncButtonLabel(true, 'OPEN')).toBe('重试并重置熔断');
  });

  it('revalidates the connection when the last probe failed or never ran', () => {
    expect(siteSyncNeedsReset(state('FAILED', 'CLOSED'), 'site-1')).toBe(true);
    expect(siteSyncNeedsReset(state('UNTESTED', 'CLOSED'), 'site-1')).toBe(true);
    expect(siteSyncButtonLabel(true, 'CLOSED')).toBe('重新验证并同步');
  });

  it('ignores cached detail belonging to another site', () => {
    expect(siteSyncNeedsReset(state('FAILED', 'OPEN', 'site-2'), 'site-1')).toBe(false);
    expect(siteSyncNeedsReset(null, 'site-1')).toBe(false);
  });
});
