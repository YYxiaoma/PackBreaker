import { defineStore } from 'pinia';
import { ref } from 'vue';

import {
  createNotificationChannel,
  deleteNotificationChannel,
  listNotificationChannels,
  setNotificationChannelEnabled,
  testNotificationChannel,
  updateNotificationChannel,
  type NotificationChannel,
  type NotificationChannelCreateInput,
  type NotificationChannelUpdateInput,
} from '../api/notifications';
import { ApiProblem, toApiProblem } from '../api/client';

export const useNotificationStore = defineStore('notifications', () => {
  const items = ref<NotificationChannel[]>([]);
  const loading = ref(false);
  const busy = ref<Record<string, boolean>>({});
  const error = ref<ApiProblem | null>(null);

  function replace(item: NotificationChannel): void {
    const index = items.value.findIndex((current) => current.id === item.id);
    if (index >= 0) items.value[index] = item;
    else items.value.unshift(item);
  }

  async function guarded<T>(key: string, action: () => Promise<T>): Promise<T> {
    busy.value = { ...busy.value, [key]: true };
    try {
      const result = await action();
      error.value = null;
      return result;
    } catch (caught) {
      const problem = toApiProblem(caught);
      error.value = problem;
      if (problem.status === 401) items.value = [];
      throw problem;
    } finally {
      const next = { ...busy.value };
      delete next[key];
      busy.value = next;
    }
  }

  async function refresh(): Promise<void> {
    loading.value = true;
    try {
      items.value = await listNotificationChannels();
      error.value = null;
    } catch (caught) {
      const problem = toApiProblem(caught);
      error.value = problem;
      if (problem.status === 401) items.value = [];
      throw problem;
    } finally {
      loading.value = false;
    }
  }

  async function create(payload: NotificationChannelCreateInput): Promise<NotificationChannel> {
    return guarded('create', async () => {
      const created = await createNotificationChannel(payload);
      replace(created);
      return created;
    });
  }

  async function update(
    channel: NotificationChannel,
    payload: NotificationChannelUpdateInput,
  ): Promise<NotificationChannel> {
    return guarded(`update:${channel.id}`, async () => {
      const updated = await updateNotificationChannel(channel, payload);
      replace(updated);
      return updated;
    });
  }

  async function remove(channel: NotificationChannel): Promise<void> {
    return guarded(`delete:${channel.id}`, async () => {
      await deleteNotificationChannel(channel);
      items.value = items.value.filter((item) => item.id !== channel.id);
    });
  }

  async function testConnection(channel: NotificationChannel): Promise<void> {
    return guarded(`test:${channel.id}`, async () => {
      await testNotificationChannel(channel.id);
      await refresh();
    });
  }

  async function setEnabled(channel: NotificationChannel, enabled: boolean): Promise<void> {
    return guarded(`enable:${channel.id}`, async () => {
      replace(await setNotificationChannelEnabled(channel, enabled));
    });
  }

  return {
    items,
    loading,
    busy,
    error,
    refresh,
    create,
    update,
    remove,
    testConnection,
    setEnabled,
  };
});
