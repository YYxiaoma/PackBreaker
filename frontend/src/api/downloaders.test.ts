import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from './client';
import {
  deleteDownloader,
  diagnoseDownloaderPaths,
  setDownloaderEnabled,
  updateDownloader,
  type Downloader,
} from './downloaders';

const downloader: Downloader = {
  id: 'downloader-synthetic-001',
  name: '测试 qB',
  type: 'QBITTORRENT',
  base_url: 'http://qb.invalid:8080',
  credential_configured: true,
  monitor_rules: {},
  path_mappings: [{ remote_prefix: '/downloads', container_prefix: '/data/downloads' }],
  capabilities: {},
  connection_status: 'UNTESTED',
  path_mapping_status: 'UNTESTED',
  enabled: false,
  version: 7,
  last_test_at: null,
  last_path_diagnostic_at: null,
  created_at: '2026-09-09T00:00:00Z',
  updated_at: '2026-09-09T00:00:00Z',
};

afterEach(() => vi.restoreAllMocks());

describe('下载器 API 并发前置条件', () => {
  it('PATCH 使用当前 version 生成 If-Match', async () => {
    const patch = vi.spyOn(apiClient, 'patch').mockResolvedValue({ data: downloader });
    await updateDownloader(downloader.id, 7, { name: '新名称' });
    expect(patch).toHaveBeenCalledWith(
      `/downloaders/${downloader.id}`,
      { name: '新名称' },
      { headers: { 'If-Match': '"7"' } },
    );
  });

  it('删除和启用都携带强 If-Match', async () => {
    const remove = vi.spyOn(apiClient, 'delete').mockResolvedValue({ data: undefined });
    const action = vi.spyOn(apiClient, 'post').mockResolvedValue({ data: downloader });

    await deleteDownloader(downloader.id, 7);
    await setDownloaderEnabled(downloader.id, 7, true);

    expect(remove).toHaveBeenCalledWith(`/downloaders/${downloader.id}`, {
      headers: { 'If-Match': '"7"' },
    });
    expect(action).toHaveBeenCalledWith(
      `/downloaders/${downloader.id}/actions`,
      { action: 'enable' },
      { headers: { 'If-Match': '"7"' } },
    );
  });
});

describe('路径诊断契约', () => {
  it('一次提交多条 probe，不在前端降级为单映射假通过', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: {
        status: 'ok',
        all_mappings_verified: true,
        error_code: null,
        results: [],
      },
    });
    const probes = [
      { remote_path: '/downloads/a.mkv', target_directory: '/data/seed-a' },
      { remote_path: '/archive/b.mkv', target_directory: '/data/seed-b' },
    ];

    await diagnoseDownloaderPaths(downloader.id, probes);

    expect(post).toHaveBeenCalledWith(`/downloaders/${downloader.id}/path-diagnostics`, { probes });
  });
});
