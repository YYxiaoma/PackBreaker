import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from './client';
import {
  analyzeTask,
  getTaskPreflight,
  getTaskPreflightCurrent,
  listTaskCandidates,
  listTaskUnits,
} from './tasks';

afterEach(() => vi.restoreAllMocks());

describe('任务分析 API', () => {
  it('使用编码后的 task id 读取 units/candidates/preflight', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: { items: [] } });
    await listTaskUnits('task/with slash');
    await listTaskCandidates('task/with slash');

    get.mockResolvedValueOnce({
      data: { snapshot_digest: 'abc', current: true, stale_reasons: [] },
    });
    await getTaskPreflightCurrent('task/with slash');
    get.mockResolvedValueOnce({ data: { id: 'pf', snapshot_digest: 'abc' } });
    await getTaskPreflight('task/with slash');

    expect(get.mock.calls.map((call) => call[0])).toEqual([
      '/tasks/task%2Fwith%20slash/units',
      '/tasks/task%2Fwith%20slash/candidates',
      '/tasks/task%2Fwith%20slash/preflight/current',
      '/tasks/task%2Fwith%20slash/preflight',
    ]);
  });

  it('analyze 只提交显式 source_root，不在前端构造宿主绝对路径', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: { id: 'pf', snapshot_digest: 'digest', current: true, stale_reasons: [] },
    });

    await analyzeTask('task-1', 'movie/Season.01');

    expect(post).toHaveBeenCalledWith('/tasks/task-1/actions', {
      action: 'analyze',
      source_root: 'movie/Season.01',
    });
  });
});
