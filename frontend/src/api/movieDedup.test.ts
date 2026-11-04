import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from './client';
import { executeMovieDedupPairs } from './movieDedup';

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('影片去重执行请求', () => {
  it('长时间文件操作不受全局 API 超时限制，同时保留幂等键', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({ data: { id: 'job-1' } });

    await executeMovieDedupPairs('job-1', ['pair-1', 'pair-2'], 'dedup-operation-1');

    expect(post).toHaveBeenCalledWith(
      '/movie-dedup/jobs/job-1/execute',
      { pair_ids: ['pair-1', 'pair-2'] },
      { headers: { 'Idempotency-Key': 'dedup-operation-1' }, timeout: 0 },
    );
  });
});
