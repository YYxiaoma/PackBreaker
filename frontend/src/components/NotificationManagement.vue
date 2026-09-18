<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue';
import { storeToRefs } from 'pinia';
import { Activity, Bell, Plus, Settings2, Trash2 } from '@lucide/vue';
import { ElMessage, ElMessageBox } from 'element-plus';

import { ApiProblem } from '../api/client';
import type {
  NotificationChannel,
  NotificationChannelKind,
  NotificationEventType,
  NotificationProxyInput,
  NotificationProxyPatchInput,
} from '../api/notifications';
import { useNotificationStore } from '../stores/notifications';

interface NotificationDraft {
  name: string;
  type: NotificationChannelKind;
  enabled: boolean;
  eventTypes: NotificationEventType[];
  botToken: string;
  chatId: string;
  sendKey: string;
  proxyEnabled: boolean;
  proxyHost: string;
  proxyPort: number | null;
  proxyUsername: string;
  proxyPassword: string;
  clearProxyPassword: boolean;
}

const EVENT_OPTIONS: Array<{ value: NotificationEventType; label: string }> = [
  { value: 'AUTH_LOGIN_SUCCESS', label: '登录' },
  { value: 'AUTH_PASSWORD_CHANGED', label: '修改密码' },
  { value: 'TASK_EXECUTION_RESULT', label: '任务执行结果' },
  { value: 'DOWNLOADER_CREATED', label: '添加下载器' },
  { value: 'SITE_CREATED', label: '添加站点' },
];

const store = useNotificationStore();
const { items, loading, busy, error } = storeToRefs(store);
const dialogVisible = ref(false);
const editing = ref<NotificationChannel | null>(null);
const replaceCredential = ref(false);
const draft = reactive<NotificationDraft>({
  name: '',
  type: 'TELEGRAM' as NotificationChannelKind,
  enabled: false,
  eventTypes: EVENT_OPTIONS.map((item) => item.value),
  botToken: '',
  chatId: '',
  sendKey: '',
  proxyEnabled: false,
  proxyHost: '',
  proxyPort: null,
  proxyUsername: '',
  proxyPassword: '',
  clearProxyPassword: false,
});

onMounted(() => void store.refresh().catch(() => undefined));

function resetCredentialSecrets(): void {
  draft.botToken = '';
  draft.chatId = '';
  draft.sendKey = '';
}

function resetSecrets(): void {
  resetCredentialSecrets();
  draft.proxyPassword = '';
}

function openCreate(): void {
  editing.value = null;
  replaceCredential.value = true;
  Object.assign(draft, {
    name: '',
    type: 'TELEGRAM' as NotificationChannelKind,
    enabled: false,
    eventTypes: EVENT_OPTIONS.map((item) => item.value),
    proxyEnabled: false,
    proxyHost: '',
    proxyPort: null,
    proxyUsername: '',
    clearProxyPassword: false,
  });
  resetSecrets();
  dialogVisible.value = true;
}

function openEdit(channel: NotificationChannel): void {
  editing.value = channel;
  replaceCredential.value = false;
  Object.assign(draft, {
    name: channel.name,
    type: channel.type,
    enabled: channel.enabled,
    eventTypes: [...channel.event_types],
    proxyEnabled: channel.proxy_enabled,
    proxyHost: channel.proxy_host ?? '',
    proxyPort: channel.proxy_port,
    proxyUsername: channel.proxy_username ?? '',
    clearProxyPassword: false,
  });
  resetSecrets();
  dialogVisible.value = true;
}

function closeDialog(): void {
  dialogVisible.value = false;
  resetSecrets();
}

function credentialPayload(kind: NotificationChannelKind): Record<string, unknown> {
  if (kind === 'TELEGRAM') {
    if (!draft.botToken.trim() || !draft.chatId.trim())
      throw new Error('请填写 Bot Token 与 Chat ID');
    return { telegram: { bot_token: draft.botToken.trim(), chat_id: draft.chatId.trim() } };
  }
  if (!draft.sendKey.trim()) throw new Error('请填写 Server酱 SendKey');
  return { serverchan: { send_key: draft.sendKey.trim() } };
}

function proxyPayload(): NotificationProxyInput {
  if (draft.proxyEnabled && (!draft.proxyHost.trim() || draft.proxyPort === null)) {
    throw new Error('启用代理时请填写代理地址和端口');
  }
  return {
    enabled: draft.proxyEnabled,
    host: draft.proxyHost.trim() || null,
    port: draft.proxyPort,
    username: draft.proxyUsername.trim() || null,
    ...(draft.proxyPassword ? { password: draft.proxyPassword } : {}),
  };
}

function proxyPatchPayload(): NotificationProxyPatchInput {
  return {
    ...proxyPayload(),
    clear_password: draft.clearProxyPassword,
  };
}

function eventLabel(type: NotificationEventType): string {
  return EVENT_OPTIONS.find((item) => item.value === type)?.label ?? type;
}

async function sendTestMessage(): Promise<void> {
  try {
    if (editing.value) {
      await store.testConnection(editing.value);
      ElMessage.success('已使用当前保存配置发送测试消息');
      return;
    }
    await store.probeTemporary({
      type: draft.type,
      ...credentialPayload(draft.type),
      proxy: proxyPayload(),
    });
    ElMessage.success('测试消息发送成功；当前表单尚未保存');
  } catch (caught) {
    if (caught instanceof Error && !(caught instanceof ApiProblem))
      ElMessage.warning(caught.message);
    else ElMessage.error(caught instanceof ApiProblem ? caught.message : '测试消息发送失败');
  }
}

async function save(): Promise<void> {
  if (!draft.name.trim()) {
    ElMessage.warning('请输入渠道名称');
    return;
  }
  if (!draft.eventTypes.length) {
    ElMessage.warning('请至少选择一种事件通知类型');
    return;
  }
  try {
    if (editing.value === null) {
      const created = await store.create({
        name: draft.name.trim(),
        type: draft.type,
        event_types: [...draft.eventTypes],
        proxy: proxyPayload(),
        ...credentialPayload(draft.type),
      });
      if (draft.enabled) {
        try {
          await store.testConnection(created);
          const tested = store.items.find((item) => item.id === created.id);
          if (tested) await store.setEnabled(tested, true);
          ElMessage.success('通知渠道已保存、测试并启用');
        } catch (caught) {
          ElMessage.warning(
            caught instanceof ApiProblem
              ? `渠道已保存但保持停用：${caught.message}`
              : '渠道已保存但测试失败，保持停用',
          );
        }
      } else {
        ElMessage.success('通知渠道已保存并保持停用');
      }
    } else {
      const payload: Parameters<typeof store.update>[1] = {
        name: draft.name.trim(),
        event_types: [...draft.eventTypes],
        credential_action: replaceCredential.value ? 'SET' : 'KEEP',
        proxy: proxyPatchPayload(),
      };
      if (replaceCredential.value) Object.assign(payload, credentialPayload(editing.value.type));
      const updated = await store.update(editing.value, payload);
      if (!draft.enabled && updated.enabled) {
        await store.setEnabled(updated, false);
      } else if (draft.enabled && !updated.enabled) {
        if (updated.connection_status === 'OK') await store.setEnabled(updated, true);
        else ElMessage.warning('配置已保存；连接配置发生变化，请发送测试消息后再启用');
      }
      ElMessage.success('通知渠道已更新');
    }
    closeDialog();
  } catch (caught) {
    resetSecrets();
    if (caught instanceof Error && !(caught instanceof ApiProblem))
      ElMessage.warning(caught.message);
    else ElMessage.error(caught instanceof ApiProblem ? caught.message : '保存通知渠道失败');
  }
}

async function testConnection(channel: NotificationChannel): Promise<void> {
  try {
    await store.testConnection(channel);
    ElMessage.success(`${channel.name} 测试发送成功`);
  } catch (caught) {
    ElMessage.error(caught instanceof ApiProblem ? caught.message : '通知渠道测试失败');
  }
}

async function toggle(channel: NotificationChannel): Promise<void> {
  try {
    await store.setEnabled(channel, !channel.enabled);
  } catch (caught) {
    ElMessage.error(caught instanceof ApiProblem ? caught.message : '通知渠道状态更新失败');
  }
}

async function remove(channel: NotificationChannel): Promise<void> {
  try {
    await ElMessageBox.confirm(`删除通知渠道「${channel.name}」？`, '确认删除', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    });
    await store.remove(channel);
    ElMessage.success('通知渠道已删除');
  } catch (caught) {
    if (caught instanceof ApiProblem) ElMessage.error(caught.message);
  }
}

function kindLabel(kind: NotificationChannelKind): string {
  return kind === 'TELEGRAM' ? 'Telegram' : 'Server酱';
}
</script>

<template>
  <div class="notification-toolbar">
    <el-button type="primary" @click="openCreate"><Plus :size="15" />添加渠道</el-button>
  </div>
  <el-alert
    v-if="error"
    :title="error.message"
    type="error"
    :closable="false"
    class="section-space"
  />
  <div v-loading="loading" class="connection-cards">
    <article v-for="channel in items" :key="channel.id" class="panel connection-card">
      <div class="card-title">
        <span class="connection-icon"><Bell :size="24" /></span>
        <div>
          <h3>{{ channel.name }}</h3>
          <small>{{ kindLabel(channel.type) }}</small>
        </div>
        <el-switch
          :model-value="channel.enabled"
          :loading="busy[`enable:${channel.id}`]"
          :disabled="!channel.enabled && channel.connection_status !== 'OK'"
          @change="toggle(channel)"
        />
      </div>
      <dl class="config-summary">
        <dt>凭证</dt>
        <dd>{{ channel.credential_configured ? '已加密配置' : '未配置' }}</dd>
        <dt>连接状态</dt>
        <dd>
          <el-tag
            :type="
              channel.connection_status === 'OK'
                ? 'success'
                : channel.connection_status === 'FAILED'
                  ? 'danger'
                  : 'info'
            "
            >{{ channel.connection_status }}</el-tag
          >
        </dd>
        <dt>事件类型</dt>
        <dd class="notification-event-list">
          {{ channel.event_types.map(eventLabel).join('、') || '未配置' }}
        </dd>
        <dt>独立代理</dt>
        <dd>
          {{
            channel.proxy_enabled
              ? `${channel.proxy_host ?? '—'}:${channel.proxy_port ?? '—'}${
                  channel.proxy_credential_configured ? ' · 已配置认证' : ''
                }`
              : '未启用'
          }}
        </dd>
        <dt>最近测试</dt>
        <dd>
          {{ channel.last_test_at ? new Date(channel.last_test_at).toLocaleString() : '尚未测试' }}
        </dd>
      </dl>
      <div class="card-actions">
        <el-button
          size="small"
          :loading="busy[`test:${channel.id}`]"
          @click="testConnection(channel)"
        >
          <Activity :size="14" />测试发送
        </el-button>
        <el-button size="small" @click="openEdit(channel)"><Settings2 :size="14" />配置</el-button>
        <el-button
          link
          type="danger"
          :loading="busy[`delete:${channel.id}`]"
          @click="remove(channel)"
        >
          <Trash2 :size="14" />删除
        </el-button>
      </div>
    </article>
    <el-empty v-if="!loading && !items.length" description="尚未配置通知渠道" />
  </div>

  <el-dialog
    v-model="dialogVisible"
    :title="editing ? '编辑通知渠道' : '添加通知渠道'"
    width="520px"
    @closed="resetSecrets"
  >
    <el-form label-position="top">
      <el-form-item label="名称"><el-input v-model="draft.name" maxlength="80" /></el-form-item>
      <el-form-item label="类型">
        <el-select v-model="draft.type" :disabled="editing !== null">
          <el-option label="Telegram" value="TELEGRAM" />
          <el-option label="Server酱" value="SERVERCHAN" />
        </el-select>
      </el-form-item>
      <el-form-item label="状态">
        <el-radio-group v-model="draft.enabled">
          <el-radio :value="true">启用</el-radio>
          <el-radio :value="false">关闭</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="事件通知类型" required>
        <el-select v-model="draft.eventTypes" multiple collapse-tags collapse-tags-tooltip>
          <el-option
            v-for="option in EVENT_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
      </el-form-item>
      <div v-if="editing" class="setting-row">
        <div>
          <b>替换凭证</b>
          <p>关闭时保留现有凭证。</p>
        </div>
        <el-switch v-model="replaceCredential" @change="resetCredentialSecrets" />
      </div>
      <template v-if="!editing || replaceCredential">
        <template v-if="draft.type === 'TELEGRAM'">
          <el-form-item label="Bot Token"
            ><el-input
              v-model="draft.botToken"
              type="password"
              show-password
              autocomplete="new-password"
          /></el-form-item>
          <el-form-item label="Chat ID"
            ><el-input v-model="draft.chatId" type="password" show-password autocomplete="off"
          /></el-form-item>
        </template>
        <el-form-item v-else label="Server酱 SendKey">
          <el-input
            v-model="draft.sendKey"
            type="password"
            show-password
            autocomplete="new-password"
          />
        </el-form-item>
      </template>

      <el-divider content-position="left">独立代理</el-divider>
      <el-form-item>
        <el-switch v-model="draft.proxyEnabled" active-text="启用该通知渠道独立代理" />
      </el-form-item>
      <div v-if="draft.proxyEnabled" class="notification-form-grid">
        <el-form-item label="代理地址" required>
          <el-input v-model="draft.proxyHost" placeholder="127.0.0.1" />
        </el-form-item>
        <el-form-item label="代理端口" required>
          <el-input-number v-model="draft.proxyPort" :min="1" :max="65535" />
        </el-form-item>
        <el-form-item label="代理账号">
          <el-input v-model="draft.proxyUsername" autocomplete="username" />
        </el-form-item>
        <el-form-item label="代理密码">
          <el-input
            v-model="draft.proxyPassword"
            type="password"
            show-password
            autocomplete="new-password"
            :disabled="draft.clearProxyPassword"
            :placeholder="editing?.proxy_credential_configured ? '留空保持现有代理密码' : '可选'"
          />
        </el-form-item>
      </div>
      <el-checkbox
        v-if="editing?.proxy_credential_configured"
        v-model="draft.clearProxyPassword"
        @change="draft.proxyPassword = ''"
      >
        清除已保存的代理密码
      </el-checkbox>
    </el-form>
    <template #footer>
      <el-button
        :loading="busy.probe || (editing ? busy[`test:${editing.id}`] : false)"
        @click="sendTestMessage"
      >
        <Activity :size="14" />发送测试消息
      </el-button>
      <el-button @click="closeDialog">取消</el-button>
      <el-button
        type="primary"
        :loading="busy.create || (editing ? busy[`update:${editing.id}`] : false)"
        @click="save"
        >保存</el-button
      >
    </template>
  </el-dialog>
</template>

<style scoped>
.notification-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 16px;
}

.notification-event-list {
  overflow-wrap: anywhere;
}

.notification-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 16px;
}

@media (max-width: 640px) {
  .notification-form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
