import { describe, expect, it } from 'vitest';

import type { Downloader } from './api/downloaders';
import { eligibleTaskExecutionTargets, targetClientCheckRequired } from './taskExecutionTargets';

function downloader(type: Downloader['type'], overrides: Partial<Downloader> = {}): Downloader {
  return {
    id: type,
    name: type,
    type,
    base_url: 'http://127.0.0.1',
    credential_configured: true,
    monitor_rules: {},
    path_mappings: [],
    capabilities: {},
    connection_status: 'OK',
    path_mapping_status: 'OK',
    enabled: true,
    version: 1,
    last_test_at: null,
    last_path_diagnostic_at: null,
    created_at: '',
    updated_at: '',
    ...overrides,
  };
}

describe('Web 任务执行目标', () => {
  it('列出通过安全前提的 qBittorrent 和 Transmission', () => {
    expect(
      eligibleTaskExecutionTargets([
        downloader('QBITTORRENT'),
        downloader('TRANSMISSION'),
        downloader('TRANSMISSION', { id: 'disabled', enabled: false }),
        downloader('TRANSMISSION', { id: 'unreachable', connection_status: 'FAILED' }),
        downloader('TRANSMISSION', { id: 'unmapped', path_mapping_status: 'UNTESTED' }),
      ]).map((item) => item.id),
    ).toEqual(['QBITTORRENT', 'TRANSMISSION']);
  });

  it('Transmission 的真实客户端校验不可跳过', () => {
    expect(targetClientCheckRequired(downloader('TRANSMISSION'), false)).toBe(true);
    expect(targetClientCheckRequired(downloader('QBITTORRENT'), true)).toBe(true);
    expect(targetClientCheckRequired(downloader('QBITTORRENT'), false)).toBe(false);
  });
});
