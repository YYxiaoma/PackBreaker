import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from './client';
import {
  deleteNotificationChannel,
  setNotificationChannelEnabled,
  updateNotificationChannel,
  type NotificationChannel,
} from './notifications';

const channel: NotificationChannel = {
  id: 'notification-synthetic-001',
  name: '测试 Telegram',
  type: 'TELEGRAM',
  credential_configured: true,
  task_link_base_url: 'https://packbreaker.invalid',
  aggregation_window_seconds: 300,
  connection_status: 'OK',
  enabled: false,
  version: 4,
  last_test_at: null,
  created_at: '2026-09-11T00:00:00Z',
  updated_at: '2026-09-11T00:00:00Z',
};

afterEach(() => vi.restoreAllMocks());

describe('通知渠道 API 并发前置条件', () => {
  it('更新保留凭证时使用 PUT + If-Match', async () => {
    const put = vi.spyOn(apiClient, 'put').mockResolvedValue({ data: channel });
    const payload = {
      name: '测试 Telegram',
      task_link_base_url: 'https://packbreaker.invalid',
      aggregation_window_seconds: 600,
      credential_action: 'KEEP' as const,
    };

    await updateNotificationChannel(channel, payload);

    expect(put).toHaveBeenCalledWith(`/notification-channels/${channel.id}`, payload, {
      headers: { 'If-Match': '"4"' },
    });
  });

  it('删除和启用都携带强 If-Match', async () => {
    const remove = vi.spyOn(apiClient, 'delete').mockResolvedValue({ data: undefined });
    const action = vi.spyOn(apiClient, 'post').mockResolvedValue({ data: channel });

    await deleteNotificationChannel(channel);
    await setNotificationChannelEnabled(channel, true);

    expect(remove).toHaveBeenCalledWith(`/notification-channels/${channel.id}`, {
      headers: { 'If-Match': '"4"' },
    });
    expect(action).toHaveBeenCalledWith(
      `/notification-channels/${channel.id}/actions`,
      { action: 'enable' },
      { headers: { 'If-Match': '"4"' } },
    );
  });
});
