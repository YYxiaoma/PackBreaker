import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from './client';
import {
  analyzeTask,
  createTaskUnitExecutionPlan,
  createTask,
  getTaskPreflight,
  getTaskPreflightCurrent,
  getTaskUnitDecision,
  getTaskUnitExecutionGate,
  getTaskUnitExecutionPlan,
  getTaskUnitReviewVerification,
  listTaskCandidates,
  listTasks,
  listTaskUnits,
  reverifyTaskUnitDecision,
  refreshTaskUnitExecutionGate,
  submitTaskUnitDecision,
} from './tasks';

afterEach(() => vi.restoreAllMocks());

describe('任务分析 API', () => {
  it('任务集合使用真实 /tasks，并原样返回创建幂等结果', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValueOnce({ data: { items: [] } });
    const post = vi.spyOn(apiClient, 'post').mockResolvedValueOnce({
      data: {
        created: true,
        item: {
          id: 'task-1',
          type: 'PACKAGE_UNPACK',
          source_downloader_id: 'downloader-1',
          source_hash: 'source-hash',
          normalized_unit_key: 'unit-key',
          status: 'PENDING',
          error_code: null,
          version: 1,
          created_at: '2026-09-10T00:00:00Z',
          updated_at: '2026-09-10T00:00:00Z',
        },
      },
    });

    await listTasks('PENDING');
    const result = await createTask({
      task_type: 'PACKAGE_UNPACK',
      source_downloader_id: 'downloader-1',
      source_hash: 'source-hash',
      normalized_unit_key: 'unit-key',
    });

    expect(get).toHaveBeenCalledWith('/tasks', { params: { status: 'PENDING' } });
    expect(post).toHaveBeenCalledWith('/tasks', {
      task_type: 'PACKAGE_UNPACK',
      source_downloader_id: 'downloader-1',
      source_hash: 'source-hash',
      normalized_unit_key: 'unit-key',
    });
    expect(result.created).toBe(true);
    expect(result.item.id).toBe('task-1');
  });

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

  it('审核 revision 使用编码后的 unit id 且保留 expected_version', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: { version: 1 } });
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({ data: { version: 2 } });

    await getTaskUnitDecision('unit/with slash');
    await submitTaskUnitDecision('unit/with slash', {
      expected_version: 1,
      approved_candidate_id: 'candidate-1',
      rejected_candidate_ids: ['candidate-2'],
      manual_mappings: [],
      note: 'review',
    });

    expect(get).toHaveBeenCalledWith('/task-units/unit%2Fwith%20slash/decision');
    expect(post).toHaveBeenCalledWith('/task-units/unit%2Fwith%20slash/decision', {
      expected_version: 1,
      approved_candidate_id: 'candidate-1',
      rejected_candidate_ids: ['candidate-2'],
      manual_mappings: [],
      note: 'review',
    });
  });

  it('审核重验证使用独立只读证据路径与 reverify 动作', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({
      data: { id: 'verification-1', verification_level: 'FULL_VERIFIED' },
    });
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: { id: 'verification-1', verification_level: 'FULL_VERIFIED' },
    });

    await getTaskUnitReviewVerification('unit/with slash');
    await reverifyTaskUnitDecision('unit/with slash');

    expect(get).toHaveBeenCalledWith('/task-units/unit%2Fwith%20slash/decision/verification');
    expect(post).toHaveBeenCalledWith('/task-units/unit%2Fwith%20slash/decision/actions', {
      action: 'reverify',
    });
  });

  it('execution gate 使用独立证据路径且刷新不携带执行参数', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({
      data: { id: 'gate-1', eligible: true, side_effects_started: false },
    });
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: { id: 'gate-1', eligible: true, side_effects_started: false },
    });

    await getTaskUnitExecutionGate('unit/with slash');
    await refreshTaskUnitExecutionGate('unit/with slash');

    expect(get).toHaveBeenCalledWith('/task-units/unit%2Fwith%20slash/execution-gate');
    expect(post).toHaveBeenCalledWith('/task-units/unit%2Fwith%20slash/execution-gate');
  });

  it('execution plan 只提交 /data 相对 target_root', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValueOnce({
      data: { id: 'plan-1', plan_digest: 'digest', current: true, ready: true },
    });
    const post = vi.spyOn(apiClient, 'post').mockResolvedValueOnce({
      data: { id: 'plan-1', plan_digest: 'digest', current: true, ready: true },
    });

    await getTaskUnitExecutionPlan('unit/with slash');
    await createTaskUnitExecutionPlan('unit/with slash', 'seeding/movies');

    expect(get).toHaveBeenCalledWith('/task-units/unit%2Fwith%20slash/execution-plan');
    expect(post).toHaveBeenCalledWith('/task-units/unit%2Fwith%20slash/execution-plan', {
      target_root: 'seeding/movies',
    });
  });
});
