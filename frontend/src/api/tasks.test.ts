import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from './client';
import {
  analyzeTask,
  cancelTask,
  createTaskUnitExecutionPlan,
  createTask,
  executeTask,
  getOperationMaintenanceReport,
  getTaskPreflight,
  getTaskPreflightCurrent,
  getTaskUnitDecision,
  getTaskUnitExecutionGate,
  getTaskUnitExecutionPlan,
  getTaskUnitRepairPlan,
  getTaskUnitReviewVerification,
  listTaskCandidates,
  listTaskEvents,
  listTaskOperations,
  listTasks,
  listTaskUnits,
  reverifyTaskUnitDecision,
  reconcileTaskOperation,
  refreshTaskUnitExecutionGate,
  submitTaskUnitDecision,
  taskEventStreamUrl,
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

  it('任务事件历史与 SSE URL 都绑定编码后的 task id 和 after_event_id', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: { items: [] } });

    await listTaskEvents('task/with slash', 'event/with slash', 50);

    expect(get).toHaveBeenCalledWith('/tasks/task%2Fwith%20slash/events', {
      params: { after_event_id: 'event/with slash', limit: 50 },
    });
    expect(taskEventStreamUrl('task/with slash', 'event/with slash')).toBe(
      '/api/v1/tasks/task%2Fwith%20slash/events/stream?after_event_id=event%2Fwith+slash',
    );
  });

  it('operation 摘要与对账动作使用编码后的 task/journal 且显式携带幂等键', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: { items: [] } });
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: {
        action: 'reconcile',
        task_id: 'task/with slash',
        journal_id: 'journal/with slash',
        kind: 'FILESYSTEM_HARDLINK',
        status: 'APPLIED',
        operation_replayed: false,
        idempotency_replayed: false,
        receipt_id: 'receipt-1',
      },
    });

    await listTaskOperations('task/with slash');
    await reconcileTaskOperation('task/with slash', 'journal/with slash', 'reconcile-key');

    expect(get).toHaveBeenCalledWith('/tasks/task%2Fwith%20slash/operations');
    expect(post).toHaveBeenCalledWith(
      '/tasks/task%2Fwith%20slash/operations/journal%2Fwith%20slash/actions',
      { action: 'reconcile' },
      { headers: { 'Idempotency-Key': 'reconcile-key' } },
    );
  });

  it('全局清理对账报告使用只读 endpoint 并携带显式 limit', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({
      data: {
        generated_at: '2026-09-12T00:00:00Z',
        summary: {
          total_journals: 4,
          attention_required: 3,
          reconcile_supported: 1,
          manual_only: 2,
          retention_candidates: 1,
          truncated: false,
        },
        repair_items: [],
        cleanup_candidates: [],
      },
    });

    const report = await getOperationMaintenanceReport(25);

    expect(get).toHaveBeenCalledWith('/operations/maintenance-report', {
      params: { limit: 25 },
    });
    expect(report.summary.manual_only).toBe(2);
    await expect(getOperationMaintenanceReport(0)).rejects.toThrow('limit 必须位于 1..500');
  });

  it('execute/cancel 使用生成 schema 并显式携带 Idempotency-Key', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: {
        action: 'execute',
        task_id: 'task-1',
        status: 'ADDING',
        task_version: 7,
        execution_plan_id: 'plan-1',
        operation_replayed: false,
        idempotency_replayed: false,
        receipt_id: 'receipt-1',
      },
    });

    await executeTask('task-1', 'plan-1', 'execute-key');
    await cancelTask(
      'task-1',
      { remove_downloader_task: true, rollback_created_resources: false },
      'cancel-key',
    );

    expect(post.mock.calls[0]).toEqual([
      '/tasks/task-1/actions',
      { action: 'execute', execution_plan_id: 'plan-1' },
      { headers: { 'Idempotency-Key': 'execute-key' } },
    ]);
    expect(post.mock.calls[1]).toEqual([
      '/tasks/task-1/actions',
      {
        action: 'cancel',
        remove_downloader_task: true,
        rollback_created_resources: false,
      },
      { headers: { 'Idempotency-Key': 'cancel-key' } },
    ]);
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

  it('execution plan 同时冻结 /data 相对 target_root 与目标下载器', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValueOnce({
      data: { id: 'plan-1', plan_digest: 'digest', current: true, ready: true },
    });
    const post = vi.spyOn(apiClient, 'post').mockResolvedValueOnce({
      data: { id: 'plan-1', plan_digest: 'digest', current: true, ready: true },
    });

    await getTaskUnitExecutionPlan('unit/with slash');
    await createTaskUnitExecutionPlan('unit/with slash', 'seeding/movies', 'qb-target');

    expect(get).toHaveBeenCalledWith('/task-units/unit%2Fwith%20slash/execution-plan');
    expect(post).toHaveBeenCalledWith('/task-units/unit%2Fwith%20slash/execution-plan', {
      target_root: 'seeding/movies',
      target_downloader_id: 'qb-target',
    });
  });

  it('repair plan 只提交模式，不接受客户端暂停/inode/ownership 证据', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValueOnce({
      data: {
        task_id: 'task-1',
        task_unit_id: 'unit/with slash',
        execution_plan_id: 'plan-1',
        downloader_kind: 'QBITTORRENT',
        evidence_source: 'CLIENT_VERIFICATION_INCOMPLETE',
        mode: 'FILE_ONLY',
        torrent_kind: 'V1',
        affected_pieces: [],
        cross_file_pieces: [],
        affected_files: [],
        actions: [],
        isolation_bytes_required: 0,
        estimated_download_bytes_upper_bound: 0,
        required_free_bytes: 0,
        available_bytes: 1024,
        downloader_paused: true,
        blocked_reasons: [],
        ready: true,
        execution_allowed: false,
      },
    });

    const result = await getTaskUnitRepairPlan('unit/with slash', 'FILE_ONLY');

    expect(get).toHaveBeenCalledWith('/task-units/unit%2Fwith%20slash/repair-plan', {
      params: { mode: 'FILE_ONLY' },
    });
    expect(result.execution_allowed).toBe(false);
  });
});
