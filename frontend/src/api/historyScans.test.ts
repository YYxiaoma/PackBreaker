import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from './client';
import {
  analyzeHistoryScanTasks,
  cancelHistoryScan,
  createHistoryScan,
  listHistoryScans,
  listHistoryScanTasks,
  materializeHistoryScan,
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
    await cancelHistoryScan('scan/with slash', 9);
    await materializeHistoryScan('scan/with slash', 10, 75);

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
    expect(post).toHaveBeenNthCalledWith(
      3,
      '/history-scans/scan%2Fwith%20slash/actions',
      { action: 'cancel', limit: 100 },
      { headers: { 'If-Match': '"9"' } },
    );
    expect(post).toHaveBeenNthCalledWith(
      4,
      '/history-scans/scan%2Fwith%20slash/actions',
      { action: 'materialize', limit: 75 },
      { headers: { 'If-Match': '"10"' } },
    );
  });

  it('历史任务结果与批量 Analyze 使用扫描派生 endpoint，不提交 source_root', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValueOnce({ data: { items: [] } });
    const post = vi.spyOn(apiClient, 'post').mockResolvedValueOnce({
      data: {
        action: 'analyze',
        attempted_count: 1,
        succeeded_count: 1,
        failed_count: 0,
        skipped_count: 0,
        items: [],
      },
    });

    await listHistoryScanTasks('scan/with slash', 250);
    await analyzeHistoryScanTasks('scan/with slash', [' task-1 ', 'task-1', 'task-2']);

    expect(get).toHaveBeenCalledWith('/history-scans/scan%2Fwith%20slash/tasks', {
      params: { limit: 250 },
    });
    expect(post).toHaveBeenCalledWith('/history-scans/scan%2Fwith%20slash/tasks/actions', {
      action: 'analyze',
      task_ids: ['task-1', 'task-2'],
    });
  });

  it('历史任务列表把状态、转换结果和查询词交给服务端筛选', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValueOnce({ data: { items: [] } });

    await listHistoryScanTasks('scan-1', 10, {
      taskStatus: 'RETRY',
      materializationStatus: 'MATERIALIZED',
      query: ' S01E01 ',
    });

    expect(get).toHaveBeenCalledWith('/history-scans/scan-1/tasks', {
      params: {
        limit: 10,
        task_status: 'RETRY',
        materialization_status: 'MATERIALIZED',
        query: 'S01E01',
      },
    });
  });
});
