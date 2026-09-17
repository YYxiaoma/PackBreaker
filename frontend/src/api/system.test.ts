import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from './client';
import {
  exportSystemDiagnostics,
  exportSystemLogs,
  getSystemHealth,
  getSystemUpgradeStatus,
  getReleasePreflight,
  getBackupPolicy,
  listSystemLogs,
  runBackupNow,
  startSystemUpgrade,
  updateBackupPolicy,
} from './system';

afterEach(() => vi.restoreAllMocks());

describe('系统运维 API', () => {
  it('读取 typed system health', async () => {
    const health = {
      status: 'ok' as const,
      generated_at: '2026-09-14T09:00:00Z',
      version: '0.1.2',
      checks: [],
    };
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: health, headers: {} });

    await expect(getSystemHealth()).resolves.toEqual(health);
    expect(get).toHaveBeenCalledWith('/system/health');
  });

  it('发布预检读取真实本地只读报告', async () => {
    const report = {
      status: 'ready' as const,
      app_version: '0.1.2',
      checks: [
        {
          name: 'database',
          status: 'ok' as const,
          code: 'DATABASE_OK',
          detail: 'SQLite integrity_check 与 migration head 通过',
        },
      ],
    };
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: report, headers: {} });

    await expect(getReleasePreflight()).resolves.toEqual(report);
    expect(get).toHaveBeenCalledWith('/system/release/preflight');
  });

  it('Docker 升级状态读取正式 digest，写动作携带幂等键', async () => {
    const digest = `sha256:${'8'.repeat(64)}`;
    const status = {
      current_version: '0.1.2',
      latest_version: '0.1.3',
      update_available: true,
      target_tag: 'v0.1.3',
      target_image_digest: digest,
      immutable_image: `ghcr.io/yyxiaoma/packbreaker@${digest}`,
      platform: 'linux/amd64',
      release_error_code: null,
      helper_available: true,
      helper_status: null,
      can_upgrade: true,
      blocked_reasons: [],
    };
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: status, headers: {} });
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: {
        request_id: 'upgrade-001',
        current_version: '0.1.2',
        target_version: '0.1.3',
        target_image: status.immutable_image,
        backup_database_file: 'packbreaker-test.db',
        helper_status: {
          protocol_version: 1,
          helper_version: '0.1.2',
          phase: 'accepted',
          message: 'accepted',
          request_id: 'upgrade-001',
          current_version: '0.1.2',
          target_version: '0.1.3',
          target_image: status.immutable_image,
          backup_database_file: 'packbreaker-test.db',
          started_at: null,
          updated_at: null,
          finished_at: null,
          rollback_performed: false,
        },
        idempotency_replayed: false,
      },
      headers: {},
    });

    await expect(getSystemUpgradeStatus()).resolves.toEqual(status);
    await startSystemUpgrade(
      { action: 'upgrade', target_version: '0.1.3', target_image_digest: digest },
      'upgrade-001',
    );

    expect(get).toHaveBeenCalledWith('/system/upgrade');
    expect(post).toHaveBeenCalledWith(
      '/system/upgrade/actions',
      { action: 'upgrade', target_version: '0.1.3', target_image_digest: digest },
      { headers: { 'Idempotency-Key': 'upgrade-001' } },
    );
  });

  it('备份策略更新使用强 If-Match，立即备份只提交动作枚举', async () => {
    const policy = {
      enabled: false,
      interval_hours: 24,
      retention_days: 30,
      keep_latest: 3,
      version: 7,
      last_attempt_at: null,
      last_success_at: null,
      last_error_code: null,
      driver_running: true,
      driver_consecutive_errors: 0,
    };
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: policy, headers: {} });
    const put = vi.spyOn(apiClient, 'put').mockResolvedValue({
      data: { ...policy, enabled: true, version: 8 },
      headers: {},
    });
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: {
        created: true,
        skipped_reason: null,
        created_at: '2026-09-14T09:00:00Z',
        database_file: 'packbreaker-test.db',
        database_size_bytes: 4096,
        retention_deleted_count: 0,
        retention_blocked_count: 0,
        retention_error_code: null,
      },
      headers: {},
    });

    await getBackupPolicy();
    await updateBackupPolicy(7, {
      enabled: true,
      interval_hours: 12,
      retention_days: 14,
      keep_latest: 4,
    });
    await runBackupNow();

    expect(get).toHaveBeenCalledWith('/system/backups/policy');
    expect(put).toHaveBeenCalledWith(
      '/system/backups/policy',
      { enabled: true, interval_hours: 12, retention_days: 14, keep_latest: 4 },
      { headers: { 'If-Match': '"7"' } },
    );
    expect(post).toHaveBeenCalledWith('/system/backups/actions', { action: 'run_now' });
  });

  it('日志查询只把受控筛选参数交给后端', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({
      data: {
        window_minutes: 60,
        limit: 200,
        count: 0,
        truncated: false,
        max_file_bytes: 2097152,
        backup_count: 4,
        approximate_capacity_bytes: 10485760,
        items: [],
      },
      headers: {},
    });

    await listSystemLogs({
      window_minutes: 360,
      limit: 100,
      level: 'WARNING',
      q: 'trace-safe',
      source: 'TASK_EVENT',
      execution_id: 'execution-1',
      trace_id: 'trace-1',
    });

    expect(get).toHaveBeenCalledWith('/system/logs', {
      params: {
        window_minutes: 360,
        limit: 100,
        level: 'WARNING',
        q: 'trace-safe',
        source: 'TASK_EVENT',
        execution_id: 'execution-1',
        trace_id: 'trace-1',
      },
    });
  });

  it('诊断与日志导出保留后端给出的安全文件名', async () => {
    const blob = new Blob(['safe']);
    const get = vi
      .spyOn(apiClient, 'get')
      .mockResolvedValueOnce({
        data: blob,
        headers: {
          'content-disposition': 'attachment; filename="packbreaker-diagnostics-test.zip"',
        },
      })
      .mockResolvedValueOnce({
        data: blob,
        headers: { 'content-disposition': 'attachment; filename="packbreaker-logs-test.json"' },
      });

    const diagnostics = await exportSystemDiagnostics();
    const logs = await exportSystemLogs({ window_minutes: 15, level: 'ERROR' });

    expect(diagnostics.filename).toBe('packbreaker-diagnostics-test.zip');
    expect(logs.filename).toBe('packbreaker-logs-test.json');
    expect(get.mock.calls[0]).toEqual(['/system/diagnostics/export', { responseType: 'blob' }]);
    expect(get.mock.calls[1]).toEqual([
      '/system/logs/export',
      { params: { window_minutes: 15, level: 'ERROR' }, responseType: 'blob' },
    ]);
  });
});
