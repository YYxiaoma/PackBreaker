<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { Bell, CheckCheck, KeyRound, LogOut, Monitor, Moon, Sun, UserRound } from '@lucide/vue';
import { ElMessage } from 'element-plus';

import { ApiProblem } from '../api/client';
import {
  getAdminInboxUnreadCount,
  listAdminInbox,
  markAdminInboxRead,
  markAllAdminInboxRead,
  type AdminInboxNotification,
} from '../api/notifications';
import { getSystemHealth } from '../api/system';
import { useAuthStore } from '../stores/auth';

type ThemeMode = 'light' | 'dark' | 'system';

const props = defineProps<{
  modelValue: boolean;
  username: string;
  themeMode: ThemeMode;
}>();
const emit = defineEmits<{
  'update:modelValue': [value: boolean];
  'update:themeMode': [value: ThemeMode];
  'unread-change': [value: number];
  logout: [];
}>();

const auth = useAuthStore();
const inbox = ref<AdminInboxNotification[]>([]);
const unreadCount = ref(0);
const inboxLoading = ref(false);
const version = ref('—');
const passwordDialogVisible = ref(false);
const currentPassword = ref('');
const newPassword = ref('');
const confirmation = ref('');

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
});
const initial = computed(() => props.username.trim().charAt(0).toUpperCase() || 'A');

watch(
  () => props.modelValue,
  (value) => {
    if (value) void refresh();
  },
);

async function refresh(): Promise<void> {
  inboxLoading.value = true;
  try {
    const [items, count, health] = await Promise.all([
      listAdminInbox(false),
      getAdminInboxUnreadCount(),
      getSystemHealth(),
    ]);
    inbox.value = items;
    unreadCount.value = count;
    version.value = health.version;
    emit('unread-change', count);
  } catch (caught) {
    ElMessage.error(caught instanceof ApiProblem ? caught.message : '读取用户信息失败');
  } finally {
    inboxLoading.value = false;
  }
}

async function markRead(item: AdminInboxNotification): Promise<void> {
  if (item.read_at) return;
  try {
    const updated = await markAdminInboxRead(item.id);
    inbox.value = inbox.value.map((current) => (current.id === updated.id ? updated : current));
    unreadCount.value = Math.max(0, unreadCount.value - 1);
    emit('unread-change', unreadCount.value);
  } catch (caught) {
    ElMessage.error(caught instanceof ApiProblem ? caught.message : '更新通知状态失败');
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
    ElMessage.error(caught instanceof ApiProblem ? caught.message : '全部标记已读失败');
  }
}

function resetPasswordForm(): void {
  currentPassword.value = '';
  newPassword.value = '';
  confirmation.value = '';
}

async function changePassword(): Promise<void> {
  if (currentPassword.value.length < 12 || newPassword.value.length < 12) {
    ElMessage.warning('管理员密码至少需要 12 个字符');
    return;
  }
  if (newPassword.value !== confirmation.value) {
    ElMessage.warning('两次输入的新密码不一致');
    return;
  }
  if (newPassword.value === currentPassword.value) {
    ElMessage.warning('新密码不能与当前密码相同');
    return;
  }
  try {
    await auth.changePassword(currentPassword.value, newPassword.value, confirmation.value);
    passwordDialogVisible.value = false;
    visible.value = false;
    resetPasswordForm();
    ElMessage.success('密码已修改，全部管理会话已撤销，请重新登录');
  } catch (caught) {
    ElMessage.error(caught instanceof ApiProblem ? caught.message : '修改密码失败');
  }
}

function severityType(severity: AdminInboxNotification['severity']): 'info' | 'warning' | 'danger' {
  if (severity === 'ERROR') return 'danger';
  if (severity === 'WARNING') return 'warning';
  return 'info';
}

function formatTime(value: string): string {
  return new Date(value).toLocaleString();
}
</script>

<template>
  <el-drawer v-model="visible" title="管理员" size="390px" class="user-menu-drawer">
    <div class="user-drawer-profile">
      <span class="user-drawer-avatar">{{ initial }}</span>
      <div>
        <strong>{{ username }}</strong>
        <small>PackBreaker v{{ version }}</small>
      </div>
    </div>

    <section class="user-drawer-section">
      <div class="user-drawer-section-title"><Monitor :size="16" />主题</div>
      <el-radio-group
        :model-value="themeMode"
        @change="emit('update:themeMode', $event as ThemeMode)"
      >
        <el-radio-button value="light"><Sun :size="14" />浅色</el-radio-button>
        <el-radio-button value="dark"><Moon :size="14" />深色</el-radio-button>
        <el-radio-button value="system"><Monitor :size="14" />跟随系统</el-radio-button>
      </el-radio-group>
    </section>

    <section class="user-drawer-section">
      <div class="user-drawer-section-heading">
        <div class="user-drawer-section-title">
          <Bell :size="16" />通知中心
          <el-badge v-if="unreadCount" :value="unreadCount" />
        </div>
        <el-button v-if="unreadCount" link type="primary" @click="markAllRead">
          <CheckCheck :size="14" />全部已读
        </el-button>
      </div>
      <div v-loading="inboxLoading" class="admin-inbox-list">
        <button
          v-for="item in inbox"
          :key="item.id"
          class="admin-inbox-item"
          :class="{ unread: !item.read_at }"
          @click="markRead(item)"
        >
          <span class="admin-inbox-dot"></span>
          <span class="admin-inbox-copy">
            <span class="admin-inbox-title-row">
              <strong>{{ item.title }}</strong>
              <el-tag size="small" :type="severityType(item.severity)">{{ item.severity }}</el-tag>
            </span>
            <span>{{ item.message }}</span>
            <small>{{ formatTime(item.created_at) }}</small>
          </span>
        </button>
        <el-empty
          v-if="!inboxLoading && !inbox.length"
          description="暂无站内通知"
          :image-size="60"
        />
      </div>
    </section>

    <section class="user-drawer-actions">
      <el-button @click="passwordDialogVisible = true"><KeyRound :size="15" />修改密码</el-button>
      <el-button type="danger" plain @click="emit('logout')"
        ><LogOut :size="15" />退出登录</el-button
      >
    </section>

    <el-dialog
      v-model="passwordDialogVisible"
      title="修改管理员密码"
      width="460px"
      append-to-body
      @closed="resetPasswordForm"
    >
      <el-form label-position="top">
        <el-form-item label="当前密码" required>
          <el-input
            v-model="currentPassword"
            type="password"
            show-password
            autocomplete="current-password"
          />
        </el-form-item>
        <el-form-item label="新密码" required>
          <el-input
            v-model="newPassword"
            type="password"
            show-password
            autocomplete="new-password"
          />
        </el-form-item>
        <el-form-item label="确认新密码" required>
          <el-input
            v-model="confirmation"
            type="password"
            show-password
            autocomplete="new-password"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="passwordDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="auth.submitting" @click="changePassword">
          修改并重新登录
        </el-button>
      </template>
    </el-dialog>
  </el-drawer>
</template>

<style scoped>
.user-drawer-profile {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 2px 0 20px;
  border-bottom: 1px solid var(--line);
}
.user-drawer-avatar {
  width: 46px;
  height: 46px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: var(--blue);
  color: white;
  font-size: 18px;
  font-weight: 750;
}
.user-drawer-profile div {
  display: grid;
  gap: 4px;
}
.user-drawer-profile small,
.admin-inbox-copy small {
  color: var(--muted);
}
.user-drawer-section {
  padding: 20px 0;
  border-bottom: 1px solid var(--line);
}
.user-drawer-section-heading,
.user-drawer-section-title,
.admin-inbox-title-row {
  display: flex;
  align-items: center;
}
.user-drawer-section-heading {
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.user-drawer-section-title {
  gap: 7px;
  font-weight: 700;
  margin-bottom: 12px;
}
.user-drawer-section-heading .user-drawer-section-title {
  margin-bottom: 0;
}
.admin-inbox-list {
  min-height: 70px;
  max-height: 360px;
  overflow: auto;
  display: grid;
  gap: 8px;
}
.admin-inbox-item {
  width: 100%;
  display: grid;
  grid-template-columns: 8px 1fr;
  gap: 9px;
  padding: 11px;
  text-align: left;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface);
  color: inherit;
  cursor: pointer;
}
.admin-inbox-item.unread {
  background: color-mix(in srgb, var(--blue) 6%, var(--surface));
}
.admin-inbox-dot {
  width: 7px;
  height: 7px;
  margin-top: 6px;
  border-radius: 50%;
  background: transparent;
}
.admin-inbox-item.unread .admin-inbox-dot {
  background: var(--blue);
}
.admin-inbox-copy {
  min-width: 0;
  display: grid;
  gap: 5px;
  line-height: 1.5;
}
.admin-inbox-title-row {
  justify-content: space-between;
  gap: 8px;
}
.admin-inbox-copy > span:not(.admin-inbox-title-row) {
  color: var(--muted);
  font-size: 13px;
}
.user-drawer-actions {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  padding-top: 20px;
}
.user-drawer-actions .el-button + .el-button {
  margin-left: 0;
}
</style>
