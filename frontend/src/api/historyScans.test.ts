import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from './client';
import {
  createHistoryScan,
  listHistoryScans,
  scanHistoryBatch,
  startHistoryScan,
} from './historyScans';

afterEach(() => vi.restoreAllMocks());

describe('历史扫描 API', () => {
  it('列表与创建使用真实 history-scans endpoint', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValueOnce({ data: { items: [] } });
    const post = vi.spyOn(apiClient, 'post').mockResolvedValueOnce({ data: { id: 'scan-1' } });

    await listHistoryScans();
    await createHistoryScan({
      root_path: '/data/movies',
      media_kind: 'MOVIE',
      extensions: ['.mkv'],
      exclude_patterns: ['sample'],
    });

    expect(get).toHaveBeenCalledWith('/history-scans');
    expect(post).toHaveBeenCalledWith('/history-scans', {
      root_path: '/data/movies',
      media_kind: 'MOVIE',
      extensions: ['.mkv'],
      exclude_patterns: ['sample'],
    });
  });

  it('状态推进编码 scan id 并携带强 If-Match', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: { scan: { id: 'scan-1' }, processed_count: 1, has_more: false },
    });

    await startHistoryScan('scan/with slash', 7);
    await scanHistoryBatch('scan/with slash', 8, 250);

    expect(post).toHaveBeenNthCalledWith(
      1,
      '/history-scans/scan%2Fwith%20slash/actions',
      { action: 'start', limit: 100 },
      { headers: { 'If-Match': '"7"' } },
    );
    expect(post).toHaveBeenNthCalledWith(
      2,
      '/history-scans/scan%2Fwith%20slash/actions',
      { action: 'scan', limit: 250 },
      { headers: { 'If-Match': '"8"' } },
    );
  });
});
