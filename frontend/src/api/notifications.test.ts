import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from './client';
import {
  deleteNotificationChannel,
  getAdminInboxUnreadCount,
  listAdminInbox,
  markAdminInboxRead,
  markAllAdminInboxRead,
  probeNotificationChannel,
  setNotificationChannelEnabled,
  updateNotificationChannel,
  type NotificationChannel,
  type NotificationChannelUpdateInput,
} from './notifications';

const channel: NotificationChannel = {
  id: 'notification-synthetic-001',
  name: '测试 Telegram',
  type: 'TELEGRAM',
  credential_configured: true,
  event_types: ['TASK_EXECUTION_RESULT'],
  proxy_enabled: false,
  proxy_host: null,
  proxy_port: null,
  proxy_username: null,
  proxy_credential_configured: false,
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
    const payload: NotificationChannelUpdateInput = {
      name: '测试 Telegram',
      event_types: ['TASK_EXECUTION_RESULT'],
      credential_action: 'KEEP',
      proxy: { enabled: false },
    };

    await updateNotificationChannel(channel, payload);

    expect(put).toHaveBeenCalledWith(`/notification-channels/${channel.id}`, payload, {
      headers: { 'If-Match': '"4"' },
    });
  });

  it('未保存渠道测试使用独立 probe 接口', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: { status: 'ok', type: 'TELEGRAM', tested_at: '2026-09-17T00:00:00Z' },
    });
    const payload = {
      type: 'TELEGRAM' as const,
      telegram: { bot_token: '123:synthetic', chat_id: '456' },
      proxy: { enabled: false },
    };

    await probeNotificationChannel(payload);

    expect(post).toHaveBeenCalledWith('/notification-channels/probe', payload);
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

  it('站内通知使用独立 Inbox 接口并支持已读动作', async () => {
    const get = vi.spyOn(apiClient, 'get').mockImplementation(async (url) => {
      if (url === '/notifications/inbox/unread-count') return { data: { count: 2 } };
      return {
        data: {
          items: [
            {
              id: 'notice-1',
              event_type: 'AUTH_LOGIN_SUCCESS',
              title: '管理员登录',
              message: 'synthetic',
              severity: 'INFO',
              read_at: null,
              created_at: '2026-09-17T00:00:00Z',
            },
          ],
        },
      };
    });
    const post = vi.spyOn(apiClient, 'post').mockImplementation(async (url) => {
      if (url === '/notifications/inbox/actions') return { data: { updated: 2 } };
      return {
        data: {
          id: 'notice-1',
          event_type: 'AUTH_LOGIN_SUCCESS',
          title: '管理员登录',
          message: 'synthetic',
          severity: 'INFO',
          read_at: '2026-09-17T00:01:00Z',
          created_at: '2026-09-17T00:00:00Z',
        },
      };
    });

    expect(await listAdminInbox(false)).toHaveLength(1);
    expect(await getAdminInboxUnreadCount()).toBe(2);
    expect((await markAdminInboxRead('notice-1')).read_at).not.toBeNull();
    expect(await markAllAdminInboxRead()).toBe(2);

    expect(get).toHaveBeenCalledWith('/notifications/inbox', {
      params: { unread_only: false },
    });
    expect(post).toHaveBeenCalledWith('/notifications/inbox/notice-1/actions', {
      action: 'mark_read',
    });
  });
});
