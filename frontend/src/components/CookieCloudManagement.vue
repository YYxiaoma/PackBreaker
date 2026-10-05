<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { Cloud, RefreshCw, ShieldCheck } from '@lucide/vue';
import { ElMessage } from 'element-plus';

import { ApiProblem, toApiProblem } from '../api/client';
import {
  getCookieCloudSettings,
  previewCookieCloudCron,
  syncCookieCloud,
  testCookieCloud,
  updateCookieCloudSettings,
  type CookieCloudCronPreview,
  type CookieCloudSettings,
} from '../api/cookiecloud';

type CronInputMode = 'VISUAL' | 'CRON';
type CronVisualKind = 'EVERY_MINUTES' | 'EVERY_HOURS' | 'DAILY' | 'WEEKLY' | 'MONTHLY';

interface Draft {
  enabled: boolean;
  serverUrl: string;
  uuid: string;
  password: string;
  clearPassword: boolean;
  autoSync: boolean;
  syncCronExpression: string;
  requestTimeoutSeconds: number;
}

const settings = ref<CookieCloudSettings>();
const loading = ref(false);
const saving = ref(false);
const probing = ref(false);
const syncing = ref(false);
const cronInputMode = ref<CronInputMode>('VISUAL');
const cronVisualKind = ref<CronVisualKind>('EVERY_MINUTES');
const cronVisualInterval = ref(30);
const cronVisualHour = ref(3);
const cronVisualMinute = ref(0);
const cronVisualWeekday = ref(1);
const cronVisualMonthDay = ref(1);
const cronPreview = ref<CookieCloudCronPreview | null>(null);
const cronPreviewLoading = ref(false);
const cronPreviewError = ref('');
let cronPreviewTimer: ReturnType<typeof setTimeout> | null = null;
let cronPreviewSequence = 0;

const draft = reactive<Draft>({
  enabled: false,
  serverUrl: '',
  uuid: '',
  password: '',
  clearPassword: false,
  autoSync: true,
  syncCronExpression: '*/30 * * * *',
  requestTimeoutSeconds: 15,
});

const connectionType = computed(() => {
  if (settings.value?.connection_status === 'OK') return 'success';
  if (settings.value?.connection_status === 'FAILED') return 'danger';
  return 'info';
});
const connectionText = computed(() => {
  if (settings.value?.connection_status === 'OK') return '连接与解密正常';
  if (settings.value?.connection_status === 'FAILED') return '上次测试或同步失败';
  return '尚未测试';
});
const syncType = computed(() => {
  if (settings.value?.last_sync_status === 'SUCCESS') return 'success';
  if (settings.value?.last_sync_status === 'FAILED') return 'danger';
  return 'info';
});
const syncText = computed(() => {
  if (settings.value?.last_sync_status === 'SUCCESS') return '上次同步成功';
  if (settings.value?.last_sync_status === 'FAILED') {
    return '上次同步失败 · ' + (settings.value.last_sync_error_code ?? 'UNKNOWN');
  }
  return '尚未同步';
});
const insecureHttp = computed(() => draft.serverUrl.trim().toLowerCase().startsWith('http://'));

watch(
  () => [
    cronInputMode.value,
    cronVisualKind.value,
    cronVisualInterval.value,
    cronVisualHour.value,
    cronVisualMinute.value,
    cronVisualWeekday.value,
    cronVisualMonthDay.value,
  ],
  () => {
    if (cronInputMode.value === 'VISUAL') applyVisualCron();
  },
);

watch(
  () => [draft.autoSync, draft.syncCronExpression],
  () => scheduleCronPreview(),
);

function applyVisualCron(): void {
  const interval = Math.max(1, Math.trunc(cronVisualInterval.value || 1));
  const hour = Math.min(23, Math.max(0, Math.trunc(cronVisualHour.value || 0)));
  const minute = Math.min(59, Math.max(0, Math.trunc(cronVisualMinute.value || 0)));
  const weekday = Math.min(6, Math.max(0, Math.trunc(cronVisualWeekday.value || 0)));
  const monthDay = Math.min(31, Math.max(1, Math.trunc(cronVisualMonthDay.value || 1)));
  if (cronVisualKind.value === 'EVERY_MINUTES') {
    draft.syncCronExpression = `*/${Math.min(59, interval)} * * * *`;
  } else if (cronVisualKind.value === 'EVERY_HOURS') {
    draft.syncCronExpression = `${minute} */${Math.min(23, interval)} * * *`;
  } else if (cronVisualKind.value === 'DAILY') {
    draft.syncCronExpression = `${minute} ${hour} * * *`;
  } else if (cronVisualKind.value === 'WEEKLY') {
    draft.syncCronExpression = `${minute} ${hour} * * ${weekday}`;
  } else {
    draft.syncCronExpression = `${minute} ${hour} ${monthDay} * *`;
  }
}

function hydrateVisualCron(expression: string): void {
  const parts = expression.trim().split(/\s+/);
  if (parts.length !== 5) {
    cronInputMode.value = 'CRON';
    return;
  }
  const [minute, hour, day, month, weekday] = parts;
  if (minute?.startsWith('*/') && hour === '*' && day === '*' && month === '*' && weekday === '*') {
    cronVisualKind.value = 'EVERY_MINUTES';
    cronVisualInterval.value = Number(minute.slice(2)) || 30;
    cronInputMode.value = 'VISUAL';
    return;
  }
  if (hour?.startsWith('*/') && day === '*' && month === '*' && weekday === '*') {
    cronVisualKind.value = 'EVERY_HOURS';
    cronVisualInterval.value = Number(hour.slice(2)) || 1;
    cronVisualMinute.value = Number(minute) || 0;
    cronInputMode.value = 'VISUAL';
    return;
  }
  if (
    day === '*' &&
    month === '*' &&
    weekday === '*' &&
    /^\d+$/.test(minute ?? '') &&
    /^\d+$/.test(hour ?? '')
  ) {
    cronVisualKind.value = 'DAILY';
    cronVisualMinute.value = Number(minute);
    cronVisualHour.value = Number(hour);
    cronInputMode.value = 'VISUAL';
    return;
  }
  if (
    day === '*' &&
    month === '*' &&
    /^\d+$/.test(weekday ?? '') &&
    /^\d+$/.test(minute ?? '') &&
    /^\d+$/.test(hour ?? '')
  ) {
    cronVisualKind.value = 'WEEKLY';
    cronVisualMinute.value = Number(minute);
    cronVisualHour.value = Number(hour);
    cronVisualWeekday.value = Number(weekday);
    cronInputMode.value = 'VISUAL';
    return;
  }
  if (
    month === '*' &&
    weekday === '*' &&
    /^\d+$/.test(day ?? '') &&
    /^\d+$/.test(minute ?? '') &&
    /^\d+$/.test(hour ?? '')
  ) {
    cronVisualKind.value = 'MONTHLY';
    cronVisualMinute.value = Number(minute);
    cronVisualHour.value = Number(hour);
    cronVisualMonthDay.value = Number(day);
    cronInputMode.value = 'VISUAL';
    return;
  }
  cronInputMode.value = 'CRON';
}

function scheduleCronPreview(): void {
  if (cronPreviewTimer !== null) clearTimeout(cronPreviewTimer);
  cronPreviewTimer = setTimeout(() => void refreshCronPreview(), 300);
}

async function refreshCronPreview(): Promise<void> {
  const expression = draft.syncCronExpression.trim();
  const sequence = ++cronPreviewSequence;
  if (!draft.autoSync || !expression) {
    cronPreview.value = null;
    cronPreviewError.value = '';
    cronPreviewLoading.value = false;
    return;
  }
  cronPreviewLoading.value = true;
  try {
    const result = await previewCookieCloudCron(expression);
    if (sequence !== cronPreviewSequence) return;
    cronPreview.value = result;
    cronPreviewError.value = '';
  } catch (caught) {
    if (sequence !== cronPreviewSequence) return;
    cronPreview.value = null;
    cronPreviewError.value = toApiProblem(caught).message;
  } finally {
    if (sequence === cronPreviewSequence) cronPreviewLoading.value = false;
  }
}

function applySettings(value: CookieCloudSettings): void {
  settings.value = value;
  Object.assign(draft, {
    enabled: value.enabled,
    serverUrl: value.server_url,
    uuid: value.uuid,
    password: '',
    clearPassword: false,
    autoSync: value.auto_sync,
    syncCronExpression: value.sync_cron_expression,
    requestTimeoutSeconds: value.request_timeout_seconds,
  });
  hydrateVisualCron(value.sync_cron_expression);
  scheduleCronPreview();
}

function errorText(caught: unknown, fallback: string): string {
  return caught instanceof ApiProblem ? caught.message : fallback;
}

function formatTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function validate(): boolean {
  if (draft.enabled && !draft.serverUrl.trim()) {
    ElMessage.warning('启用 CookieCloud 前请输入服务器地址');
    return false;
  }
  if (draft.enabled && !draft.uuid.trim()) {
    ElMessage.warning('启用 CookieCloud 前请输入 UUID');
    return false;
  }
  if (
    draft.enabled &&
    !draft.password &&
    (!settings.value?.password_configured || draft.clearPassword)
  ) {
    ElMessage.warning('启用 CookieCloud 前必须配置密码');
    return false;
  }
  if (draft.autoSync && !draft.syncCronExpression.trim()) {
    ElMessage.warning('启用自动同步时必须配置 Cron 表达式');
    return false;
  }
  if (draft.autoSync && cronPreviewError.value) {
    ElMessage.warning('请先修正 Cron 表达式');
    return false;
  }
  return true;
}

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    applySettings(await getCookieCloudSettings());
  } catch (caught) {
    ElMessage.error(errorText(caught, 'CookieCloud 配置读取失败'));
  } finally {
    loading.value = false;
  }
}

async function save(): Promise<void> {
  if (!settings.value || !validate()) return;
  saving.value = true;
  try {
    const passwordAction = draft.clearPassword ? 'CLEAR' : draft.password ? 'SET' : 'KEEP';
    const updated = await updateCookieCloudSettings(settings.value, {
      enabled: draft.enabled,
      server_url: draft.serverUrl.trim(),
      uuid: draft.uuid.trim(),
      password_action: passwordAction,
      ...(passwordAction === 'SET' ? { password: draft.password } : {}),
      auto_sync: draft.autoSync,
      sync_cron_expression: draft.syncCronExpression.trim(),
      request_timeout_seconds: draft.requestTimeoutSeconds,
    });
    applySettings(updated);
    ElMessage.success('CookieCloud 配置已保存');
  } catch (caught) {
    ElMessage.error(errorText(caught, 'CookieCloud 配置保存失败'));
  } finally {
    saving.value = false;
  }
}

async function probe(): Promise<void> {
  probing.value = true;
  try {
    const result = await testCookieCloud();
    await refresh();
    ElMessage.success(
      '连接与解密正常：' + result.domain_count + ' 个域名，' + result.cookie_count + ' 条 Cookie',
    );
  } catch (caught) {
    await refresh();
    ElMessage.error(errorText(caught, 'CookieCloud 测试失败'));
  } finally {
    probing.value = false;
  }
}

async function syncNow(): Promise<void> {
  syncing.value = true;
  try {
    const result = await syncCookieCloud();
    await refresh();
    ElMessage.success(
      `同步完成：读取 ${result.source_domains} 个域名 / ${result.source_cookies} 条 Cookie，` +
        `可同步站点 ${result.eligible_sites}，匹配 ${result.matched_sites}，` +
        `更新 ${result.updated_sites}，无变化 ${result.unchanged_sites}，未匹配域名 ${result.unmatched_domains}`,
    );
  } catch (caught) {
    await refresh();
    ElMessage.error(errorText(caught, 'CookieCloud 同步失败'));
  } finally {
    syncing.value = false;
  }
}

onMounted(() => void refresh());
</script>

<template>
  <div v-loading="loading" class="cookiecloud-management">
    <div class="settings-section-heading">
      <div>
        <h3>CookieCloud</h3>
        <p>
          对接 easychen/CookieCloud。PackBreaker 只向服务器发送 UUID，密码仅在本机 SecretStore
          中用于解密。
        </p>
      </div>
      <Cloud :size="24" aria-hidden="true" />
    </div>

    <div v-if="settings" class="cookiecloud-status-grid">
      <div class="cookiecloud-status-item">
        <span>连接状态</span>
        <el-tag :type="connectionType" effect="plain">{{ connectionText }}</el-tag>
        <small>最后测试：{{ formatTime(settings.last_test_at) }}</small>
      </div>
      <div class="cookiecloud-status-item">
        <span>同步状态</span>
        <el-tag :type="syncType" effect="plain">{{ syncText }}</el-tag>
        <small>最后同步：{{ formatTime(settings.last_sync_at) }}</small>
      </div>
      <div class="cookiecloud-status-item cookiecloud-status-wide">
        <span>最近同步统计</span>
        <strong>{{ settings.updated_sites }} / {{ settings.matched_sites }}</strong>
        <small>
          更新 / 匹配 · 可同步站点 {{ settings.eligible_sites }} · 无变化
          {{ settings.unchanged_sites }}
        </small>
        <small>
          源数据 {{ settings.source_domains }} 个域名 / {{ settings.source_cookies }} 条 Cookie ·
          未匹配域名 {{ settings.unmatched_domains }}
        </small>
      </div>
    </div>

    <el-alert
      v-if="insecureHttp"
      type="warning"
      :closable="false"
      title="当前使用 HTTP。仅建议在受信任的内网或反向代理后使用；公网 CookieCloud 请优先使用 HTTPS。"
      class="cookiecloud-alert"
    />

    <el-form label-position="top" class="cookiecloud-form">
      <el-form-item label="启用 CookieCloud">
        <el-switch v-model="draft.enabled" />
      </el-form-item>
      <div class="cookiecloud-form-grid">
        <el-form-item label="服务器地址">
          <el-input
            v-model="draft.serverUrl"
            placeholder="https://cookie.example.com 或 https://host/api"
          />
        </el-form-item>
        <el-form-item label="UUID">
          <el-input v-model="draft.uuid" autocomplete="off" placeholder="CookieCloud UUID" />
        </el-form-item>
      </div>
      <el-form-item label="密码">
        <el-input
          v-model="draft.password"
          type="password"
          show-password
          autocomplete="new-password"
          :placeholder="settings?.password_configured ? '已配置；留空保持不变' : 'CookieCloud 密码'"
        />
        <el-checkbox
          v-if="settings?.password_configured"
          v-model="draft.clearPassword"
          :disabled="Boolean(draft.password)"
          >清除已保存密码</el-checkbox
        >
      </el-form-item>

      <div class="cookiecloud-form-grid">
        <el-form-item label="自动同步">
          <el-switch v-model="draft.autoSync" />
        </el-form-item>
        <el-form-item label="请求超时（秒）">
          <el-input-number v-model="draft.requestTimeoutSeconds" :min="1" :max="120" />
        </el-form-item>
      </div>

      <el-form-item v-if="draft.autoSync" label="自动同步时间">
        <div class="cron-editor">
          <el-radio-group v-model="cronInputMode" size="small">
            <el-radio-button value="VISUAL">图形化</el-radio-button>
            <el-radio-button value="CRON">Cron</el-radio-button>
          </el-radio-group>

          <template v-if="cronInputMode === 'VISUAL'">
            <div class="cron-visual-row">
              <el-select v-model="cronVisualKind" class="cron-kind-select">
                <el-option label="每隔 N 分钟" value="EVERY_MINUTES" />
                <el-option label="每隔 N 小时" value="EVERY_HOURS" />
                <el-option label="每天固定时间" value="DAILY" />
                <el-option label="每周" value="WEEKLY" />
                <el-option label="每月" value="MONTHLY" />
              </el-select>
              <el-input-number
                v-if="cronVisualKind === 'EVERY_MINUTES' || cronVisualKind === 'EVERY_HOURS'"
                v-model="cronVisualInterval"
                :min="1"
                :max="cronVisualKind === 'EVERY_MINUTES' ? 59 : 23"
              />
              <el-select v-if="cronVisualKind === 'WEEKLY'" v-model="cronVisualWeekday">
                <el-option label="周日" :value="0" />
                <el-option label="周一" :value="1" />
                <el-option label="周二" :value="2" />
                <el-option label="周三" :value="3" />
                <el-option label="周四" :value="4" />
                <el-option label="周五" :value="5" />
                <el-option label="周六" :value="6" />
              </el-select>
              <el-input-number
                v-if="cronVisualKind === 'MONTHLY'"
                v-model="cronVisualMonthDay"
                :min="1"
                :max="31"
              />
              <template v-if="cronVisualKind !== 'EVERY_MINUTES'">
                <el-input-number v-model="cronVisualHour" :min="0" :max="23" />
                <span>:</span>
                <el-input-number v-model="cronVisualMinute" :min="0" :max="59" />
              </template>
            </div>
            <el-input v-model="draft.syncCronExpression" readonly />
          </template>
          <el-input v-else v-model="draft.syncCronExpression" placeholder="例如：0 */6 * * *" />

          <small v-if="cronPreviewLoading" class="field-hint">正在计算执行时间…</small>
          <small v-else-if="cronPreviewError" class="field-error">{{ cronPreviewError }}</small>
          <div v-else-if="cronPreview" class="cron-preview">
            <small class="field-hint">
              {{ cronPreview.description }} · 时区 {{ cronPreview.timezone }}
            </small>
            <small class="field-hint">
              未来 5 次：{{ cronPreview.next_runs.map((item) => formatTime(item)).join(' · ') }}
            </small>
          </div>
        </div>
      </el-form-item>
    </el-form>

    <el-alert
      type="info"
      :closable="false"
      title="手动“立即同步”不受 Cron 限制；Cron 仅控制后台自动同步。同步统计会区分源数据、可同步站点、匹配、更新、无变化和未匹配域名。"
    />

    <div class="cookiecloud-actions">
      <el-button :loading="probing" :disabled="!settings?.password_configured" @click="probe">
        <ShieldCheck :size="16" /> 测试连接
      </el-button>
      <el-button :loading="syncing" :disabled="!settings?.password_configured" @click="syncNow">
        <RefreshCw :size="16" /> 立即同步
      </el-button>
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
    </div>
  </div>
</template>

<style scoped>
.cookiecloud-management {
  display: grid;
  gap: 20px;
}

.settings-section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.settings-section-heading h3 {
  margin: 0 0 6px;
}

.settings-section-heading p {
  margin: 0;
  color: var(--el-text-color-secondary);
  line-height: 1.65;
}

.cookiecloud-status-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.cookiecloud-status-item {
  display: grid;
  gap: 8px;
  padding: 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
}

.cookiecloud-status-item span,
.cookiecloud-status-item small,
.field-hint {
  color: var(--el-text-color-secondary);
}

.cookiecloud-status-item strong {
  font-size: 20px;
}

.cookiecloud-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.cookiecloud-alert {
  margin: 0;
}

.cron-editor,
.cron-preview {
  display: grid;
  width: 100%;
  gap: 10px;
}

.cron-visual-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.cron-kind-select {
  width: 180px;
}

.field-error {
  color: var(--el-color-danger);
}

.cookiecloud-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

@media (max-width: 900px) {
  .cookiecloud-status-grid,
  .cookiecloud-form-grid {
    grid-template-columns: 1fr;
  }

  .cookiecloud-actions {
    flex-wrap: wrap;
  }
}
</style>
