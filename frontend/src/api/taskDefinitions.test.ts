import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from './client';
import {
  browseTaskDirectories,
  createTaskDefinition,
  createTaskDefinitionExecutionPlan,
  executeManualTaskDefinition,
  getTaskDefinitionExecution,
  listTaskDefinitionExecutions,
  listTaskDefinitions,
  precheckTaskDefinition,
  previewTaskCron,
  previewTaskDirectory,
  retryFailedTaskDefinitionExecution,
  scanMonitorTaskDefinition,
  setTaskDefinitionPaused,
} from './taskDefinitions';
import type { TaskDefinitionCreateInput } from './taskDefinitions';

afterEach(() => vi.restoreAllMocks());

describe('v0.1.5 任务定义 API', () => {
  it('列表可按手动/监控任务类型过滤', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: { items: [] } });

    await listTaskDefinitions('MONITOR');

    expect(get).toHaveBeenCalledWith('/task-definitions', { params: { kind: 'MONITOR' } });
  });

  it('创建任务显式提交名称、扫描站点、Cron 与默认过滤策略', async () => {
    const payload: TaskDefinitionCreateInput = {
      name: '夜间扫描',
      kind: 'MONITOR',
      site_id: 'site-1',
      source: { kind: 'DIRECTORY', directory_path: 'downloads' },
      cron_expression: '0 */2 * * *',
      filters: {
        file_types: ['VIDEO'],
        video_extensions: ['.mkv', '.mp4'],
        archive_extensions: ['.rar'],
        min_size_bytes: null,
        max_size_bytes: null,
        include_name: null,
        exclude_names: ['sample', 'trailer'],
        ignore_temp_files: true,
        temp_patterns: ['*.part'],
        include_subdirectories: true,
        max_scan_depth: null,
      },
      output_policy: {
        output_directory: 'output',
        storage_mode: 'HARDLINK',
        preserve_structure: true,
        conflict_policy: 'VERIFY_REUSE_OR_STOP',
      },
      execution_policy: {
        stability_detection_enabled: true,
        stability_wait_seconds: 60,
        only_completed_downloads: true,
        initial_scope: 'NEW_ONLY',
        debounce_seconds: 30,
        overlap_policy: 'SKIP',
        auto_retry_enabled: true,
        max_auto_retries: 3,
        retry_intervals_seconds: [60, 300, 900],
      },
    };
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({ data: { id: 'definition-1' } });

    await createTaskDefinition(payload);

    expect(post).toHaveBeenCalledWith('/task-definitions', payload);
  });

  it('目录浏览与扫描预览使用受控任务定义端点', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({
      data: { current_path: 'incoming', entries: [] },
    });
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: { directory_path: 'incoming', matched_count: 0, total_size_bytes: 0, files: [] },
    });
    const filters = {
      file_types: ['VIDEO'],
      video_extensions: ['.mkv'],
      archive_extensions: ['.rar'],
      min_size_bytes: null,
      max_size_bytes: null,
      include_name: null,
      exclude_names: ['sample'],
      ignore_temp_files: true,
      temp_patterns: ['*.part'],
      include_subdirectories: true,
      max_scan_depth: null,
    };

    await browseTaskDirectories('incoming');
    await previewTaskDirectory('incoming', filters);

    expect(get).toHaveBeenCalledWith('/task-definitions/source-directories', {
      params: { path: 'incoming' },
    });
    expect(post).toHaveBeenCalledWith('/task-definitions/directory-preview', {
      directory_path: 'incoming',
      filters,
    });
  });

  it('Cron 预览使用后端统一解析与时区端点', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: {
        cron_expression: '0 */2 * * *',
        timezone: 'Asia/Shanghai',
        description: '每 2 小时执行一次（第 00 分钟）',
        next_runs: [],
      },
    });

    await previewTaskCron('0 */2 * * *');

    expect(post).toHaveBeenCalledWith('/task-definitions/cron-preview', {
      cron_expression: '0 */2 * * *',
    });
  });

  it('保存前预检使用与创建任务相同的完整 payload', async () => {
    const payload: TaskDefinitionCreateInput = {
      name: '预检任务',
      kind: 'MANUAL',
      site_id: 'site-1',
      source: { kind: 'DOWNLOADER', downloader_id: 'downloader-1', config: {} },
      filters: {
        file_types: ['VIDEO'],
        video_extensions: ['.mkv'],
        archive_extensions: ['.rar'],
        min_size_bytes: null,
        max_size_bytes: null,
        include_name: null,
        exclude_names: [],
        ignore_temp_files: true,
        temp_patterns: [],
        include_subdirectories: true,
        max_scan_depth: null,
      },
      output_policy: {
        output_directory: 'output',
        storage_mode: 'HARDLINK',
        preserve_structure: true,
        conflict_policy: 'VERIFY_REUSE_OR_STOP',
      },
      execution_policy: {
        stability_detection_enabled: true,
        stability_wait_seconds: 60,
        only_completed_downloads: true,
        initial_scope: 'NEW_ONLY',
        debounce_seconds: 30,
        overlap_policy: 'SKIP',
        auto_retry_enabled: true,
        max_auto_retries: 3,
        retry_intervals_seconds: [60, 300, 900],
      },
    };
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: { status: 'OK', items: [] },
    });

    await precheckTaskDefinition(payload);

    expect(post).toHaveBeenCalledWith('/task-definitions/precheck', payload);
  });

  it('任务定义执行计划使用对象级安全桥接端点', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({ data: { id: 'plan-1' } });

    await createTaskDefinitionExecutionPlan('definition-1', 'execution-1', 'item-1');

    expect(post).toHaveBeenCalledWith(
      '/task-definitions/definition-1/executions/execution-1/items/item-1/execution-plan',
    );
  });

  it('手动执行和失败对象重试使用任务定义执行端点与幂等键', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({ data: { id: 'execution-1' } });

    await executeManualTaskDefinition('definition-1');
    await retryFailedTaskDefinitionExecution('definition-1', 'execution-1', 'retry-key-1');

    expect(post).toHaveBeenNthCalledWith(1, '/task-definitions/definition-1/executions');
    expect(post).toHaveBeenNthCalledWith(
      2,
      '/task-definitions/definition-1/executions/execution-1/retry-failed',
      undefined,
      { headers: { 'Idempotency-Key': 'retry-key-1' } },
    );
  });

  it('监控任务立即扫描使用独立扫描端点', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: { task_definition_id: 'definition-1', outcome: 'NO_CHANGES' },
    });

    await scanMonitorTaskDefinition('definition-1');

    expect(post).toHaveBeenCalledWith('/task-definitions/definition-1/scan');
  });

  it('监控任务暂停与恢复使用统一 action 端点', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({ data: { id: 'definition-1' } });

    await setTaskDefinitionPaused('definition-1', true);
    await setTaskDefinitionPaused('definition-1', false);

    expect(post).toHaveBeenNthCalledWith(1, '/task-definitions/definition-1/actions', {
      action: 'pause',
    });
    expect(post).toHaveBeenNthCalledWith(2, '/task-definitions/definition-1/actions', {
      action: 'resume',
    });
  });

  it('执行记录支持分页筛选并可读取单次执行详情', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({
      data: { items: [], page: 2, page_size: 20, total: 21 },
    });

    await listTaskDefinitionExecutions('definition-1', {
      page: 2,
      page_size: 20,
      status: 'FAILED',
      trigger: 'CRON',
      search: 'Movie',
    });
    await getTaskDefinitionExecution('definition-1', 'execution-1');

    expect(get).toHaveBeenNthCalledWith(1, '/task-definitions/definition-1/executions', {
      params: {
        page: 2,
        page_size: 20,
        status: 'FAILED',
        trigger: 'CRON',
        search: 'Movie',
      },
    });
    expect(get).toHaveBeenNthCalledWith(2, '/task-definitions/definition-1/executions/execution-1');
  });
});
