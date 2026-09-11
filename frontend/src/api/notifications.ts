import { apiClient, strongEtag } from './client';

export type NotificationChannelKind = 'TELEGRAM' | 'SERVERCHAN';
export type NotificationProbeStatus = 'UNTESTED' | 'OK' | 'FAILED';

export interface NotificationChannel {
  id: string;
  name: string;
  type: NotificationChannelKind;
  credential_configured: boolean;
  task_link_base_url: string | null;
  aggregation_window_seconds: number;
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

export interface NotificationChannelCreateInput {
  name: string;
  type: NotificationChannelKind;
  telegram?: TelegramCredentialInput;
  serverchan?: ServerChanCredentialInput;
  task_link_base_url?: string | null;
  aggregation_window_seconds: number;
}

export interface NotificationChannelUpdateInput {
  name: string;
  task_link_base_url: string | null;
  aggregation_window_seconds: number;
  credential_action: 'KEEP' | 'SET' | 'CLEAR';
  telegram?: TelegramCredentialInput;
  serverchan?: ServerChanCredentialInput;
}

export interface NotificationProbeResult {
  status: 'ok';
  type: NotificationChannelKind;
  tested_at: string;
}

export async function listNotificationChannels(): Promise<NotificationChannel[]> {
  const response = await apiClient.get<{ items: NotificationChannel[] }>('/notification-channels');
  return response.data.items;
}

export async function createNotificationChannel(
  payload: NotificationChannelCreateInput,
): Promise<NotificationChannel> {
  const response = await apiClient.post<NotificationChannel>('/notification-channels', payload);
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
