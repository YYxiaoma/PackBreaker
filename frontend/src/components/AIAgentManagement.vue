<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import {
  Bot,
  CheckCircle2,
  MessageCircle,
  RefreshCw,
  ShieldCheck,
  TriangleAlert,
} from '@lucide/vue';
import { ElMessage } from 'element-plus';

import { ApiProblem } from '../api/client';
import {
  getAIAgentSettings,
  getAIAgentStatus,
  getAITelegramBinding,
  probeAIAgent,
  updateAITelegramBinding,
  updateAIAgentSettings,
  type AIAgentSettings,
  type AIAgentStatus,
  type AIDataScope,
  type AIProviderKind,
  type AITelegramBinding,
} from '../api/aiAgent';
import { listNotificationChannels, type NotificationChannel } from '../api/notifications';

interface AIDraft {
  enabled: boolean;
  providerKind: AIProviderKind;
  baseUrl: string;
  model: string;
  apiKey: string;
  clearApiKey: boolean;
  requestTimeoutSeconds: number;
  maxContextMessages: number;
  dataScopes: AIDataScope[];
}

interface TelegramDraft {
  enabled: boolean;
  notificationChannelId: string;
  allowedChatIds: string;
  allowedUserIds: string;
  idleTimeoutMinutes: number;
  maxContextMessages: number;
}

const SCOPE_OPTIONS: Array<{ value: AIDataScope; label: string }> = [
  { value: 'SYSTEM_HEALTH', label: '系统健康' },
  { value: 'TASK_EXECUTIONS', label: '任务定义与执行记录' },
  { value: 'REDACTED_LOGS', label: '脱敏运行日志' },
  { value: 'SITE_STATUS', label: '站点连接状态' },
  { value: 'DOWNLOADER_STATUS', label: '下载器状态与运行指标' },
  { value: 'VERSION_STATUS', label: '版本与更新信息' },
  { value: 'HELP_DOCS', label: '项目内置帮助文档' },
];

const settings = ref<AIAgentSettings>();
const status = ref<AIAgentStatus>();
const telegramBinding = ref<AITelegramBinding>();
const telegramChannels = ref<NotificationChannel[]>([]);
const loading = ref(false);
const saving = ref(false);
const telegramSaving = ref(false);
const probing = ref(false);
const savedProbing = ref(false);
const draft = reactive<AIDraft>({
  enabled: false,
  providerKind: 'OPENAI',
  baseUrl: 'https://api.openai.com/v1',
  model: '',
  apiKey: '',
  clearApiKey: false,
  requestTimeoutSeconds: 30,
  maxContextMessages: 20,
  dataScopes: SCOPE_OPTIONS.map((item) => item.value),
});
const telegramDraft = reactive<TelegramDraft>({
  enabled: false,
  notificationChannelId: '',
  allowedChatIds: '',
  allowedUserIds: '',
  idleTimeoutMinutes: 60,
  maxContextMessages: 20,
});

const httpWarning = computed(
  () =>
    draft.providerKind === 'OPENAI_COMPATIBLE' &&
    draft.baseUrl.trim().toLowerCase().startsWith('http://'),
);
const statusType = computed(() => {
  if (settings.value?.connection_status === 'OK') return 'success';
  if (settings.value?.connection_status === 'FAILED') return 'danger';
  return 'info';
});
const statusText = computed(() => {
  if (settings.value?.connection_status === 'OK') return '已通过连接测试';
  if (settings.value?.connection_status === 'FAILED') return '上次连接测试失败';
  return '尚未测试';
});
const telegramStatusType = computed(() => {
  if (status.value?.telegram_consecutive_errors) return 'danger';
  if (status.value?.telegram_enabled && status.value?.telegram_driver_running) return 'success';
  return 'info';
});
const telegramStatusText = computed(() => {
  if (status.value?.telegram_consecutive_errors) {
    return `运行异常 · ${status.value.telegram_last_error_code ?? 'UNKNOWN'}`;
  }
  if (status.value?.telegram_enabled && status.value?.telegram_driver_running) {
    return 'Long Polling 运行中';
  }
  if (telegramBinding.value?.enabled) return '已启用，Driver 尚未运行';
  return '未启用';
});

function applySettings(value: AIAgentSettings): void {
  settings.value = value;
  Object.assign(draft, {
    enabled: value.enabled,
    providerKind: value.provider_kind,
    baseUrl: value.base_url,
    model: value.model,
    apiKey: '',
    clearApiKey: false,
    requestTimeoutSeconds: value.request_timeout_seconds,
    maxContextMessages: value.max_context_messages,
    dataScopes: [...value.data_scopes],
  });
}

function applyTelegram(value: AITelegramBinding): void {
  telegramBinding.value = value;
  Object.assign(telegramDraft, {
    enabled: value.enabled,
    notificationChannelId: value.notification_channel_id ?? '',
    allowedChatIds: value.allowed_chat_ids.join('\n'),
    allowedUserIds: value.allowed_user_ids.join('\n'),
    idleTimeoutMinutes: value.idle_timeout_minutes,
    maxContextMessages: value.max_context_messages,
  });
}

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    const [nextSettings, nextStatus, nextTelegram, channels] = await Promise.all([
      getAIAgentSettings(),
      getAIAgentStatus(),
      getAITelegramBinding(),
      listNotificationChannels(),
    ]);
    applySettings(nextSettings);
    status.value = nextStatus;
    applyTelegram(nextTelegram);
    telegramChannels.value = channels.filter((channel) => channel.type === 'TELEGRAM');
  } catch (caught) {
    ElMessage.error(caught instanceof ApiProblem ? caught.message : 'AI 助手配置读取失败');
  } finally {
    loading.value = false;
  }
}

function parseIds(value: string): string[] {
  return [
    ...new Set(
      value
        .split(/[\s,;]+/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];
}

function validateTelegram(): boolean {
  const chatIds = parseIds(telegramDraft.allowedChatIds);
  const userIds = parseIds(telegramDraft.allowedUserIds);
  if (telegramDraft.enabled && !telegramDraft.notificationChannelId) {
    ElMessage.warning('启用 Telegram AI 前请选择 Telegram 通知渠道');
    return false;
  }
  if (telegramDraft.enabled && !chatIds.length && !userIds.length) {
    ElMessage.warning('启用 Telegram AI 前至少填写一个允许的 Chat ID 或 User ID');
    return false;
  }
  if (chatIds.some((item) => !/^-?\d{1,20}$/.test(item))) {
    ElMessage.warning('Chat ID 必须是整数，可为 Telegram 群组负数 ID');
    return false;
  }
  if (userIds.some((item) => !/^\d{1,20}$/.test(item))) {
    ElMessage.warning('User ID 必须是正整数');
    return false;
  }
  return true;
}

function validateDraft(): boolean {
  if (!draft.model.trim()) {
    ElMessage.warning('请输入 Model');
    return false;
  }
  if (draft.providerKind === 'OPENAI_COMPATIBLE' && !draft.baseUrl.trim()) {
    ElMessage.warning('请输入 OpenAI-compatible Base URL');
    return false;
  }
  if (!draft.dataScopes.length) {
    ElMessage.warning('请至少允许 AI 读取一种数据类型');
    return false;
  }
  return true;
}

async function save(): Promise<void> {
  if (!settings.value || !validateDraft()) return;
  saving.value = true;
  try {
    const apiKeyAction = draft.clearApiKey ? 'CLEAR' : draft.apiKey ? 'SET' : 'KEEP';
    const updated = await updateAIAgentSettings(settings.value, {
      enabled: draft.enabled,
      provider_kind: draft.providerKind,
      base_url: draft.providerKind === 'OPENAI' ? null : draft.baseUrl.trim(),
      model: draft.model.trim(),
      request_timeout_seconds: draft.requestTimeoutSeconds,
      max_context_messages: draft.maxContextMessages,
      data_scopes: [...draft.dataScopes],
      api_key_action: apiKeyAction,
      ...(apiKeyAction === 'SET' ? { api_key: draft.apiKey } : {}),
    });
    applySettings(updated);
    if (draft.enabled && !updated.enabled) {
      ElMessage.warning('连接关键配置已变化，AI 助手已保持停用；请测试已保存配置后再启用');
    } else {
      ElMessage.success('AI 助手配置已保存');
    }
  } catch (caught) {
    ElMessage.error(caught instanceof ApiProblem ? caught.message : 'AI 助手配置保存失败');
  } finally {
    saving.value = false;
  }
}

async function saveTelegram(): Promise<void> {
  if (!telegramBinding.value || !validateTelegram()) return;
  telegramSaving.value = true;
  try {
    const updated = await updateAITelegramBinding(telegramBinding.value, {
      notification_channel_id: telegramDraft.notificationChannelId || null,
      enabled: telegramDraft.enabled,
      allowed_chat_ids: parseIds(telegramDraft.allowedChatIds),
      allowed_user_ids: parseIds(telegramDraft.allowedUserIds),
      idle_timeout_minutes: telegramDraft.idleTimeoutMinutes,
      max_context_messages: telegramDraft.maxContextMessages,
    });
    applyTelegram(updated);
    status.value = await getAIAgentStatus();
    ElMessage.success('Telegram AI 配置已保存');
  } catch (caught) {
    ElMessage.error(caught instanceof ApiProblem ? caught.message : 'Telegram AI 配置保存失败');
  } finally {
    telegramSaving.value = false;
  }
}

async function probeCurrent(): Promise<void> {
  if (!validateDraft()) return;
  probing.value = true;
  try {
    await probeAIAgent({
      use_saved: false,
      provider_kind: draft.providerKind,
      base_url: draft.providerKind === 'OPENAI' ? null : draft.baseUrl.trim(),
      model: draft.model.trim(),
      request_timeout_seconds: draft.requestTimeoutSeconds,
      ...(draft.apiKey ? { api_key: draft.apiKey } : {}),
    });
    ElMessage.success('当前表单 Provider 测试成功；配置尚未保存');
  } catch (caught) {
    ElMessage.error(caught instanceof ApiProblem ? caught.message : 'AI Provider 测试失败');
  } finally {
    probing.value = false;
  }
}

async function probeSaved(): Promise<void> {
  if (!settings.value?.api_key_configured || !settings.value.model) {
    ElMessage.warning('请先保存 API Key 与 Model');
    return;
  }
  savedProbing.value = true;
  try {
    await probeAIAgent({ use_saved: true });
    await refresh();
    ElMessage.success('已保存 AI Provider 配置测试成功');
  } catch (caught) {
    await refresh();
    ElMessage.error(caught instanceof ApiProblem ? caught.message : '已保存 Provider 测试失败');
  } finally {
    savedProbing.value = false;
  }
}

function providerChanged(): void {
  if (draft.providerKind === 'OPENAI') draft.baseUrl = 'https://api.openai.com/v1';
}

onMounted(() => void refresh());
</script>

<template>
  <div v-loading="loading" class="ai-settings">
    <section class="panel ai-status-panel">
      <div class="ai-status-copy">
        <span class="file-icon"><Bot :size="20" /></span>
        <div>
          <h3>AI 助手</h3>
          <p class="muted">v0.1.6 仅提供 Telegram AI 对话；本页面只负责配置和状态管理。</p>
        </div>
      </div>
      <div class="ai-status-actions">
        <el-tag :type="statusType">{{ statusText }}</el-tag>
        <el-switch v-model="draft.enabled" active-text="启用" inactive-text="停用" />
      </div>
    </section>

    <el-alert
      title="AI 助手首版严格只读"
      description="AI 可以查询健康、任务、脱敏日志、站点/下载器状态、版本和帮助文档，但不能执行拆包、修改配置、删除记录、修改密码或升级容器。"
      type="info"
      :closable="false"
      show-icon
      class="section-space"
    />

    <section class="panel section-space">
      <h3>Provider 配置</h3>
      <el-form label-position="top" class="ai-form">
        <div class="ai-form-grid">
          <el-form-item label="Provider 类型" required>
            <el-select v-model="draft.providerKind" @change="providerChanged">
              <el-option label="OpenAI" value="OPENAI" />
              <el-option label="OpenAI-compatible" value="OPENAI_COMPATIBLE" />
            </el-select>
          </el-form-item>
          <el-form-item label="Model" required>
            <el-input v-model="draft.model" maxlength="200" placeholder="例如 gpt-5.6" />
          </el-form-item>
        </div>

        <el-form-item label="API Base URL" required>
          <el-input
            v-model="draft.baseUrl"
            :disabled="draft.providerKind === 'OPENAI'"
            placeholder="https://provider.example/v1"
          />
        </el-form-item>
        <el-alert
          v-if="httpWarning"
          title="当前使用 HTTP：API Key 与对话内容不会获得传输层加密，仅适用于可信内网。"
          type="warning"
          :closable="false"
          show-icon
          class="ai-inline-alert"
        />

        <el-form-item label="API Key">
          <el-input
            v-model="draft.apiKey"
            type="password"
            show-password
            autocomplete="off"
            :disabled="draft.clearApiKey"
            :placeholder="
              settings?.api_key_configured ? '已配置；留空表示保持不变' : '输入 Provider API Key'
            "
          />
          <div class="ai-secret-meta">
            <span v-if="settings?.api_key_configured"><CheckCircle2 :size="14" /> 已安全保存</span>
            <el-checkbox v-model="draft.clearApiKey" :disabled="Boolean(draft.apiKey)">
              清除已保存 API Key
            </el-checkbox>
          </div>
        </el-form-item>

        <div class="ai-form-grid">
          <el-form-item label="请求超时（秒）">
            <el-input-number v-model="draft.requestTimeoutSeconds" :min="1" :max="120" />
          </el-form-item>
          <el-form-item label="最大上下文消息数">
            <el-input-number v-model="draft.maxContextMessages" :min="2" :max="100" />
          </el-form-item>
        </div>
      </el-form>
      <div class="ai-actions">
        <el-button :loading="probing" @click="probeCurrent">
          <RefreshCw :size="15" />测试当前配置
        </el-button>
        <el-button :loading="savedProbing" @click="probeSaved">
          <ShieldCheck :size="15" />测试已保存配置
        </el-button>
        <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
      </div>
    </section>

    <section class="panel section-space">
      <h3>AI 可读取的数据范围</h3>
      <p class="muted">
        无论如何选择，Cookie、API Key、密码、Session/CSRF、SecretStore
        密文和代理密码都不会进入模型上下文。
      </p>
      <el-checkbox-group v-model="draft.dataScopes" class="ai-scope-grid">
        <el-checkbox v-for="item in SCOPE_OPTIONS" :key="item.value" :value="item.value">
          {{ item.label }}
        </el-checkbox>
      </el-checkbox-group>
      <div class="ai-safety-note">
        <TriangleAlert :size="16" />
        OpenAI-compatible 的 Base URL 与 Model 由真实 Provider 决定是否可用，PackBreaker
        不维护模型白名单。
      </div>
    </section>

    <section class="panel section-space">
      <div class="telegram-heading">
        <div>
          <h3>Telegram AI 对话</h3>
          <p class="muted">
            复用“通知”中的 Telegram Bot Token 与代理；这里只配置双向对话权限和会话策略。
          </p>
        </div>
        <el-tag :type="telegramStatusType">{{ telegramStatusText }}</el-tag>
      </div>
      <el-alert
        title="Telegram AI 默认拒绝所有未授权来源"
        description="只有命中 Chat ID / User ID allowlist 的文本消息才会进入 AI；未授权消息不会调用 Provider 或只读 Tool。"
        type="info"
        :closable="false"
        show-icon
        class="ai-inline-alert telegram-alert"
      />
      <el-form label-position="top" class="ai-form">
        <el-form-item label="Telegram 通知渠道" required>
          <el-select
            v-model="telegramDraft.notificationChannelId"
            clearable
            placeholder="选择已配置 Bot Token 的 Telegram 渠道"
          >
            <el-option
              v-for="channel in telegramChannels"
              :key="channel.id"
              :label="`${channel.name}${channel.credential_configured ? '' : '（未配置凭证）'}`"
              :value="channel.id"
              :disabled="!channel.credential_configured"
            />
          </el-select>
        </el-form-item>

        <div class="ai-form-grid">
          <el-form-item label="允许的 Chat ID">
            <el-input
              v-model="telegramDraft.allowedChatIds"
              type="textarea"
              :rows="4"
              placeholder="-1001234567890&#10;123456789"
            />
            <div class="muted ai-field-hint">
              每行一个，也支持逗号或空格分隔；群组 Chat ID 可以是负数。
            </div>
          </el-form-item>
          <el-form-item label="允许的 User ID">
            <el-input
              v-model="telegramDraft.allowedUserIds"
              type="textarea"
              :rows="4"
              placeholder="123456789"
            />
            <div class="muted ai-field-hint">
              填写后，同一 Chat 中还必须匹配允许的 Telegram User ID。
            </div>
          </el-form-item>
        </div>

        <div class="ai-form-grid">
          <el-form-item label="会话空闲超时（分钟）">
            <el-input-number v-model="telegramDraft.idleTimeoutMinutes" :min="5" :max="10080" />
          </el-form-item>
          <el-form-item label="Telegram 上下文消息数">
            <el-input-number v-model="telegramDraft.maxContextMessages" :min="2" :max="100" />
          </el-form-item>
        </div>
      </el-form>
      <div class="telegram-footer">
        <div class="telegram-meta muted">
          <span>Cursor：{{ telegramBinding?.last_update_id ?? 0 }}</span>
          <span v-if="status?.telegram_consecutive_errors">
            连续错误：{{ status.telegram_consecutive_errors }}
          </span>
        </div>
        <div class="ai-actions">
          <el-switch
            v-model="telegramDraft.enabled"
            active-text="启用 Telegram AI"
            inactive-text="停用"
          />
          <el-button type="primary" :loading="telegramSaving" @click="saveTelegram">
            <MessageCircle :size="15" />保存 Telegram 配置
          </el-button>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.ai-settings {
  display: grid;
}
.ai-status-panel,
.ai-status-copy,
.ai-status-actions,
.ai-actions,
.ai-secret-meta,
.ai-safety-note,
.telegram-heading,
.telegram-footer,
.telegram-meta {
  display: flex;
  align-items: center;
}
.ai-status-panel {
  justify-content: space-between;
  gap: 18px;
}
.ai-status-copy {
  gap: 12px;
}
.ai-status-copy h3,
.ai-status-copy p {
  margin: 0;
}
.ai-status-actions,
.ai-actions,
.ai-secret-meta {
  gap: 12px;
}
.ai-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.ai-inline-alert {
  margin: -4px 0 18px;
}
.ai-secret-meta {
  width: 100%;
  justify-content: space-between;
  margin-top: 8px;
  color: var(--muted);
  font-size: 12px;
}
.ai-secret-meta > span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.ai-scope-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 18px;
  margin-top: 14px;
}
.ai-safety-note {
  gap: 8px;
  margin-top: 18px;
  color: var(--muted);
  font-size: 13px;
}
.telegram-heading,
.telegram-footer {
  justify-content: space-between;
  gap: 18px;
}
.telegram-heading h3,
.telegram-heading p {
  margin: 0;
}
.telegram-heading p {
  margin-top: 4px;
}
.telegram-alert {
  margin-top: 16px;
}
.telegram-meta {
  gap: 16px;
  font-size: 12px;
}
.ai-field-hint {
  width: 100%;
  margin-top: 6px;
  font-size: 12px;
}
@media (max-width: 720px) {
  .ai-status-panel,
  .ai-status-actions,
  .ai-actions,
  .ai-secret-meta,
  .telegram-heading,
  .telegram-footer,
  .telegram-meta {
    align-items: stretch;
    flex-direction: column;
  }
  .ai-form-grid,
  .ai-scope-grid {
    grid-template-columns: 1fr;
  }
}
</style>
