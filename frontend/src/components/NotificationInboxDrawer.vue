<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { Bell, CheckCheck, RefreshCw } from '@lucide/vue';
import { ElMessage } from 'element-plus';
import { ApiProblem } from '../api/client';
import {
  getAdminInboxUnreadCount,
  listAdminInbox,
  markAdminInboxRead,
  markAllAdminInboxRead,
  type AdminInboxNotification,
} from '../api/notifications';

const props = defineProps<{ modelValue: boolean }>();
const emit = defineEmits<{
  'update:modelValue': [value: boolean];
  'unread-change': [value: number];
}>();
const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
});
const inbox = ref<AdminInboxNotification[]>([]);
const unreadCount = ref(0);
const loading = ref(false);
watch(
  () => props.modelValue,
  (shown) => {
    if (shown) void refresh();
  },
);

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    const [items, count] = await Promise.all([listAdminInbox(false), getAdminInboxUnreadCount()]);
    inbox.value = items;
    unreadCount.value = count;
    emit('unread-change', count);
  } catch (caught) {
    ElMessage.error(caught instanceof ApiProblem ? caught.message : '通知读取失败');
  } finally {
    loading.value = false;
  }
}
async function markRead(item: AdminInboxNotification): Promise<void> {
  if (item.read_at) return;
  try {
    const updated = await markAdminInboxRead(item.id);
    inbox.value = inbox.value.map((entry) => (entry.id === updated.id ? updated : entry));
    unreadCount.value = Math.max(0, unreadCount.value - 1);
    emit('unread-change', unreadCount.value);
  } catch (caught) {
    ElMessage.error(caught instanceof ApiProblem ? caught.message : '更新通知失败');
  }
}
async function markAllRead(): Promise<void> {
  try {
    await markAllAdminInboxRead();
    const now = new Date().toISOString();
    inbox.value = inbox.value.map((item) => ({ ...item, read_at: item.read_at ?? now }));
    unreadCount.value = 0;
    emit('unread-change', 0);
  } catch (caught) {
    ElMessage.error(caught instanceof ApiProblem ? caught.message : '全部已读失败');
  }
}
function severityType(value: AdminInboxNotification['severity']): 'info' | 'warning' | 'danger' {
  if (value === 'ERROR') return 'danger';
  if (value === 'WARNING') return 'warning';
  return 'info';
}
</script>

<template>
  <el-drawer v-model="visible" title="消息通知" size="420px" class="notification-inbox-drawer">
    <div class="notification-toolbar">
      <span><Bell :size="17" />未读 {{ unreadCount }}</span>
      <div>
        <el-button link :loading="loading" @click="refresh"><RefreshCw :size="15" />刷新</el-button>
        <el-button v-if="unreadCount" link type="primary" @click="markAllRead">
          <CheckCheck :size="15" />全部已读
        </el-button>
      </div>
    </div>
    <div v-loading="loading" class="notification-inbox-list">
      <button
        v-for="item in inbox"
        :key="item.id"
        class="notification-inbox-item"
        :class="{ unread: !item.read_at }"
        @click="markRead(item)"
      >
        <span class="notification-dot"></span>
        <span class="notification-copy">
          <span class="notification-heading">
            <strong>{{ item.title }}</strong>
            <el-tag size="small" :type="severityType(item.severity)">{{ item.severity }}</el-tag>
          </span>
          <span>{{ item.message }}</span>
          <small>{{ new Date(item.created_at).toLocaleString() }}</small>
        </span>
      </button>
      <el-empty v-if="!loading && !inbox.length" description="暂无站内通知" :image-size="60" />
    </div>
  </el-drawer>
</template>

<style scoped>
.notification-toolbar,
.notification-toolbar > span,
.notification-heading {
  display: flex;
  align-items: center;
  gap: 9px;
}
.notification-toolbar {
  justify-content: space-between;
  padding-bottom: 18px;
  border-bottom: 1px solid var(--line);
}
.notification-inbox-list {
  display: grid;
  gap: 10px;
  padding-top: 18px;
}
.notification-inbox-item {
  display: grid;
  grid-template-columns: 8px 1fr;
  gap: 10px;
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 12px;
  background: var(--surface);
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.notification-inbox-item.unread {
  background: color-mix(in srgb, var(--blue) 6%, var(--surface));
}
.notification-dot {
  width: 7px;
  height: 7px;
  margin-top: 6px;
  border-radius: 50%;
}
.unread .notification-dot {
  background: var(--blue);
}
.notification-copy {
  min-width: 0;
  display: grid;
  gap: 6px;
  line-height: 1.5;
}
.notification-heading {
  justify-content: space-between;
}
.notification-copy > span:not(.notification-heading),
.notification-copy small {
  color: var(--muted);
}
</style>
