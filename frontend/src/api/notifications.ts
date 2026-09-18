import { apiClient, strongEtag } from './client';

export type NotificationChannelKind = 'TELEGRAM' | 'SERVERCHAN';
export type NotificationProbeStatus = 'UNTESTED' | 'OK' | 'FAILED';
export type NotificationEventType =
  | 'AUTH_LOGIN_SUCCESS'
  | 'AUTH_PASSWORD_CHANGED'
  | 'TASK_EXECUTION_RESULT'
  | 'DOWNLOADER_CREATED'
  | 'SITE_CREATED'
  | 'VERSION_UPDATE_AVAILABLE'
  | 'SITE_RELIABILITY';

export interface NotificationChannel {
  id: string;
  name: string;
  type: NotificationChannelKind;
  credential_configured: boolean;
  event_types: NotificationEventType[];
  proxy_enabled: boolean;
  proxy_host: string | null;
  proxy_port: number | null;
  proxy_username: string | null;
  proxy_credential_configured: boolean;
  connection_status: NotificationProbeStatus;
  enabled: boolean;
  version: number;
  last_test_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface TelegramCredentialInput {
  bot_token: string;
  chat_id: string;
}

export interface ServerChanCredentialInput {
  send_key: string;
}

export interface NotificationProxyInput {
  enabled: boolean;
  host?: string | null;
  port?: number | null;
  username?: string | null;
  password?: string | null;
}

export interface NotificationProxyPatchInput extends NotificationProxyInput {
  clear_password?: boolean;
}

export interface NotificationChannelCreateInput {
  name: string;
  type: NotificationChannelKind;
  telegram?: TelegramCredentialInput;
  serverchan?: ServerChanCredentialInput;
  event_types: NotificationEventType[];
  proxy: NotificationProxyInput;
}

export interface NotificationChannelUpdateInput {
  name: string;
  event_types: NotificationEventType[];
  credential_action: 'KEEP' | 'SET' | 'CLEAR';
  telegram?: TelegramCredentialInput;
  serverchan?: ServerChanCredentialInput;
  proxy: NotificationProxyPatchInput;
}

export interface NotificationTemporaryProbeInput {
  type: NotificationChannelKind;
  telegram?: TelegramCredentialInput;
  serverchan?: ServerChanCredentialInput;
  proxy: NotificationProxyInput;
}

export interface NotificationProbeResult {
  status: 'ok';
  type: NotificationChannelKind;
  tested_at: string;
}

export interface AdminInboxNotification {
  id: string;
  event_type: string;
  title: string;
  message: string;
  severity: 'INFO' | 'WARNING' | 'ERROR';
  read_at: string | null;
  created_at: string;
}

export async function listNotificationChannels(): Promise<NotificationChannel[]> {
  const response = await apiClient.get<{ items: NotificationChannel[] }>('/notification-channels');
  return response.data.items;
}

export async function listAdminInbox(unreadOnly = false): Promise<AdminInboxNotification[]> {
  const response = await apiClient.get<{ items: AdminInboxNotification[] }>(
    '/notifications/inbox',
    {
      params: { unread_only: unreadOnly },
    },
  );
  return response.data.items;
}

export async function getAdminInboxUnreadCount(): Promise<number> {
  const response = await apiClient.get<{ count: number }>('/notifications/inbox/unread-count');
  return response.data.count;
}

export async function markAdminInboxRead(id: string): Promise<AdminInboxNotification> {
  const response = await apiClient.post<AdminInboxNotification>(
    `/notifications/inbox/${id}/actions`,
    { action: 'mark_read' },
  );
  return response.data;
}

export async function markAllAdminInboxRead(): Promise<number> {
  const response = await apiClient.post<{ updated: number }>('/notifications/inbox/actions', {
    action: 'mark_all_read',
  });
  return response.data.updated;
}

export async function createNotificationChannel(
  payload: NotificationChannelCreateInput,
): Promise<NotificationChannel> {
  const response = await apiClient.post<NotificationChannel>('/notification-channels', payload);
  return response.data;
}

export async function probeNotificationChannel(
  payload: NotificationTemporaryProbeInput,
): Promise<NotificationProbeResult> {
  const response = await apiClient.post<NotificationProbeResult>(
    '/notification-channels/probe',
    payload,
  );
  return response.data;
}

export async function updateNotificationChannel(
  channel: NotificationChannel,
  payload: NotificationChannelUpdateInput,
): Promise<NotificationChannel> {
  const response = await apiClient.put<NotificationChannel>(
    `/notification-channels/${channel.id}`,
    payload,
    { headers: { 'If-Match': strongEtag(channel.version) } },
  );
  return response.data;
}

export async function deleteNotificationChannel(channel: NotificationChannel): Promise<void> {
  await apiClient.delete(`/notification-channels/${channel.id}`, {
    headers: { 'If-Match': strongEtag(channel.version) },
  });
}

export async function testNotificationChannel(id: string): Promise<NotificationProbeResult> {
  const response = await apiClient.post<NotificationProbeResult>(
    `/notification-channels/${id}/test`,
  );
  return response.data;
}

export async function setNotificationChannelEnabled(
  channel: NotificationChannel,
  enabled: boolean,
): Promise<NotificationChannel> {
  const response = await apiClient.post<NotificationChannel>(
    `/notification-channels/${channel.id}/actions`,
    { action: enabled ? 'enable' : 'disable' },
    { headers: { 'If-Match': strongEtag(channel.version) } },
  );
  return response.data;
}
