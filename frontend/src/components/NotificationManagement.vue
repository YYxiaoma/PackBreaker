<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue';
import { storeToRefs } from 'pinia';
import { Activity, Bell, Plus, Settings2, Trash2 } from '@lucide/vue';
import { ElMessage, ElMessageBox } from 'element-plus';

import { ApiProblem } from '../api/client';
import type { NotificationChannel, NotificationChannelKind } from '../api/notifications';
import { useNotificationStore } from '../stores/notifications';

const store = useNotificationStore();
const { items, loading, busy, error } = storeToRefs(store);
const dialogVisible = ref(false);
const editing = ref<NotificationChannel | null>(null);
const replaceCredential = ref(false);
const draft = reactive({
  name: '',
  type: 'TELEGRAM' as NotificationChannelKind,
  taskLinkBaseUrl: '',
  aggregationWindowSeconds: 300,
  botToken: '',
  chatId: '',
  sendKey: '',
});

onMounted(() => void store.refresh().catch(() => undefined));

function resetSecrets(): void {
  draft.botToken = '';
  draft.chatId = '';
  draft.sendKey = '';
}

function openCreate(): void {
  editing.value = null;
  replaceCredential.value = true;
  Object.assign(draft, {
    name: '',
    type: 'TELEGRAM' as NotificationChannelKind,
    taskLinkBaseUrl: '',
    aggregationWindowSeconds: 300,
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
    taskLinkBaseUrl: channel.task_link_base_url ?? '',
    aggregationWindowSeconds: channel.aggregation_window_seconds,
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

async function save(): Promise<void> {
  if (!draft.name.trim()) {
    ElMessage.warning('请输入渠道名称');
    return;
  }
  try {
    if (editing.value === null) {
      await store.create({
        name: draft.name.trim(),
        type: draft.type,
        task_link_base_url: draft.taskLinkBaseUrl.trim() || null,
        aggregation_window_seconds: draft.aggregationWindowSeconds,
        ...credentialPayload(draft.type),
      });
      ElMessage.success('通知渠道已保存；请先测试连接，再启用');
    } else {
      const payload: Parameters<typeof store.update>[1] = {
        name: draft.name.trim(),
        task_link_base_url: draft.taskLinkBaseUrl.trim() || null,
        aggregation_window_seconds: draft.aggregationWindowSeconds,
        credential_action: replaceCredential.value ? 'SET' : 'KEEP',
      };
      if (replaceCredential.value) Object.assign(payload, credentialPayload(editing.value.type));
      await store.update(editing.value, payload);
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
    ElMessage.success(`${channel.name} 真实测试发送成功`);
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
  <div class="section-heading">
    <div>
      <h3>通知渠道</h3>
      <p class="muted">真实发送 · 凭证加密保存 · 重复错误按任务聚合</p>
    </div>
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
          :disabled="channel.connection_status !== 'OK'"
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
        <dt>聚合窗口</dt>
        <dd>{{ channel.aggregation_window_seconds }} 秒</dd>
        <dt>任务链接</dt>
        <dd>{{ channel.task_link_base_url || '未配置' }}</dd>
      </dl>
      <div class="card-actions">
        <el-button
          size="small"
          :loading="busy[`test:${channel.id}`]"
          @click="testConnection(channel)"
        >
          <Activity :size="14" />真实测试
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
      <el-form-item label="任务链接基础地址">
        <el-input v-model="draft.taskLinkBaseUrl" placeholder="https://packbreaker.example" />
      </el-form-item>
      <el-form-item label="重复事件聚合窗口（秒）">
        <el-input-number v-model="draft.aggregationWindowSeconds" :min="1" :max="86400" />
      </el-form-item>
      <div v-if="editing" class="setting-row">
        <div>
          <b>替换凭证</b>
          <p>关闭时保留现有加密凭证，不会从服务端读取明文。</p>
        </div>
        <el-switch v-model="replaceCredential" @change="resetSecrets" />
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
    </el-form>
    <template #footer>
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
