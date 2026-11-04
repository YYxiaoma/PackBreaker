<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import {
  CalendarDays,
  CheckCircle2,
  Clock3,
  Cloud,
  FileText,
  RefreshCw,
  ShieldCheck,
} from '@lucide/vue';
import { ElMessage } from 'element-plus';
import { useSiteStore } from '../stores/sites';
import { waitForCookieCloudProfileSync } from '../utils/waitForCookieCloudProfileSync';
import { hasCookieCloudConnectionDraftChanged } from '../utils/cookiecloudDraft';

import { ApiProblem, toApiProblem } from '../api/client';
import {
  getCookieCloudProfileSyncStatus,
  getCookieCloudSettings,
  previewCookieCloudCron,
  syncCookieCloud,
  testCookieCloudDraft,
  updateCookieCloudSettings,
  type CookieCloudCronPreview,
  type CookieCloudSettings,
} from '../api/cookiecloud';

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
const siteStore = useSiteStore();
const loading = ref(false);
const saving = ref(false);
const probing = ref(false);
const syncing = ref(false);
const draftProbeStatus = ref<'OK' | 'FAILED' | null>(null);
const draftProbeAt = ref<string | null>(null);
let draftProbeSequence = 0;
const cronPreview = ref<CookieCloudCronPreview | null>(null);
const cronPreviewLoading = ref(false);
const cronPreviewError = ref('');

function applyCronPreset(expression: string): void {
  draft.syncCronExpression = expression;
}

let cronPreviewTimer: ReturnType<typeof setTimeout> | null = null;
let cronPreviewSequence = 0;
let componentMounted = true;

onUnmounted(() => {
  componentMounted = false;
  if (cronPreviewTimer !== null) {
    clearTimeout(cronPreviewTimer);
    cronPreviewTimer = null;
  }
});

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

const connectionDraftChanged = computed(() =>
  hasCookieCloudConnectionDraftChanged(draft, settings.value),
);
const settingsDraftChanged = computed(() => {
  const saved = settings.value;
  if (!saved) return false;
  return (
    draft.enabled !== saved.enabled ||
    draft.serverUrl.trim() !== saved.server_url ||
    draft.uuid.trim() !== saved.uuid ||
    draft.password.length > 0 ||
    draft.clearPassword ||
    draft.autoSync !== saved.auto_sync ||
    draft.syncCronExpression.trim() !== saved.sync_cron_expression ||
    draft.requestTimeoutSeconds !== saved.request_timeout_seconds
  );
});
const connectionType = computed(() => {
  if (draftProbeStatus.value === 'OK') return 'success';
  if (draftProbeStatus.value === 'FAILED') return 'danger';
  if (connectionDraftChanged.value) return 'info';
  if (settings.value?.connection_status === 'OK') return 'success';
  if (settings.value?.connection_status === 'FAILED') return 'danger';
  return 'info';
});
const connectionText = computed(() => {
  if (draftProbeStatus.value === 'OK') return '当前配置连接正常';
  if (draftProbeStatus.value === 'FAILED') return '当前配置测试失败';
  if (connectionDraftChanged.value) return '当前草稿尚未测试';
  if (settings.value?.connection_status === 'OK') return '连接与解密正常';
  if (settings.value?.connection_status === 'FAILED') return '上次测试或同步失败';
  return '尚未测试';
});
const connectionTestTime = computed(() => {
  if (draftProbeStatus.value !== null) return formatTime(draftProbeAt.value);
  if (connectionDraftChanged.value) return '—';
  return formatTime(settings.value?.last_test_at);
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
  () => [draft.autoSync, draft.syncCronExpression],
  () => scheduleCronPreview(),
);

watch(
  () => [
    draft.serverUrl,
    draft.uuid,
    draft.password,
    draft.clearPassword,
    draft.requestTimeoutSeconds,
  ],
  () => {
    draftProbeSequence += 1;
    draftProbeStatus.value = null;
    draftProbeAt.value = null;
    probing.value = false;
  },
);

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
  draftProbeStatus.value = null;
  draftProbeAt.value = null;
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

function formatRunDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: cronPreview.value?.timezone || 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(date);
}

function formatRunTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: cronPreview.value?.timezone || 'Asia/Shanghai',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date);
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

async function refresh(preserveDraft = false): Promise<void> {
  const preserveUnsavedDraft = preserveDraft && settingsDraftChanged.value;
  loading.value = true;
  try {
    const updated = await getCookieCloudSettings();
    if (preserveUnsavedDraft) {
      settings.value = updated;
      scheduleCronPreview();
    } else {
      applySettings(updated);
    }
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
  if (!draft.serverUrl.trim() || !draft.uuid.trim()) {
    ElMessage.warning('请先填写 CookieCloud 服务器地址和 UUID');
    return;
  }
  if (draft.clearPassword) {
    ElMessage.warning('已选择清除已保存密码，请取消清除后再测试连接');
    return;
  }
  if (!draft.password && !settings.value?.password_configured) {
    ElMessage.warning('请先填写 CookieCloud 密码');
    return;
  }

  const sequence = ++draftProbeSequence;
  const payload = {
    server_url: draft.serverUrl.trim(),
    uuid: draft.uuid.trim(),
    ...(draft.password ? { password: draft.password } : {}),
    request_timeout_seconds: draft.requestTimeoutSeconds,
  };
  probing.value = true;
  try {
    const result = await testCookieCloudDraft(payload);
    if (sequence !== draftProbeSequence) return;
    draftProbeStatus.value = 'OK';
    draftProbeAt.value = result.tested_at;
    ElMessage.success(
      `连接与解密正常：发现 ${result.domain_count} 个域名、${result.cookie_count} 条 Cookie`,
    );
  } catch (caught) {
    if (sequence !== draftProbeSequence) return;
    draftProbeStatus.value = 'FAILED';
    draftProbeAt.value = new Date().toISOString();
    ElMessage.error(errorText(caught, 'CookieCloud 测试失败'));
  } finally {
    if (sequence === draftProbeSequence) probing.value = false;
  }
}

async function refreshSitesAfterBackgroundSync(): Promise<void> {
  // Refresh once immediately so newly imported site configurations appear, then
  // wait for the bounded background profile queue and refresh the final statistics.
  try {
    await siteStore.refresh();
  } catch {
    // Keep polling: a transient list request should not hide the background completion.
  }
  const waitResult = await waitForCookieCloudProfileSync(
    getCookieCloudProfileSyncStatus,
    () => componentMounted,
  );
  if (!componentMounted || waitResult === 'stopped') return;
  try {
    await siteStore.refresh();
  } catch {
    // The page's regular Refresh action remains available if the list API is down.
  }
}

async function syncNow(): Promise<void> {
  syncing.value = true;
  try {
    const result = await syncCookieCloud();
    await refresh(true);
    ElMessage.success(
      `发现 ${result.discovered_sites} 个 PT 站，同步成功 ${result.sync_success_sites} 个，同步失败 ${result.sync_failed_sites} 个。`,
    );
    void refreshSitesAfterBackgroundSync();
  } catch (caught) {
    await refresh(true);
    ElMessage.error(errorText(caught, 'CookieCloud 同步失败'));
  } finally {
    syncing.value = false;
  }
}

onMounted(() => void refresh());
</script>

<template>
  <div v-loading="loading" class="cookiecloud-management">
    <header class="cookiecloud-page-heading">
      <div class="cookiecloud-title-icon"><Cloud :size="26" aria-hidden="true" /></div>
      <div>
        <h2>CookieCloud 配置</h2>
        <p>集成 easychen/CookieCloud，实现站点 Cookie 的自动同步与管理。</p>
      </div>
    </header>

    <section v-if="settings" class="cookiecloud-status-grid" aria-label="CookieCloud 状态概览">
      <article class="cookiecloud-status-card">
        <div class="status-icon status-icon-success"><CheckCircle2 :size="25" /></div>
        <div class="status-copy">
          <span>连接状态</span>
          <strong :class="`status-text-${connectionType}`">{{ connectionText }}</strong>
          <small
            >最近测试时间：{{ connectionTestTime
            }}{{
              draftProbeStatus ? ' · 当前草稿' : connectionDraftChanged ? ' · 当前草稿未测试' : ''
            }}</small
          >
        </div>
      </article>
      <article class="cookiecloud-status-card">
        <div class="status-icon status-icon-primary"><RefreshCw :size="25" /></div>
        <div class="status-copy">
          <span>同步状态</span>
          <strong :class="`status-text-${syncType}`">{{ syncText }}</strong>
          <small>最近同步时间：{{ formatTime(settings.last_sync_at) }}</small>
        </div>
      </article>
      <article class="cookiecloud-status-card cookiecloud-result-card">
        <div class="status-icon status-icon-primary"><FileText :size="25" /></div>
        <div class="status-copy cookiecloud-result-copy">
          <span>最近同步结果</span>
          <div class="cookiecloud-result-metrics">
            <div>
              <strong>{{ settings.eligible_sites }}</strong
              ><small>可同步站点</small>
            </div>
            <div>
              <strong class="result-success">{{ settings.matched_sites }}</strong
              ><small>匹配站点</small>
            </div>
            <div>
              <strong class="result-failed">{{ settings.unmatched_domains }}</strong
              ><small>未匹配域名</small>
            </div>
          </div>
        </div>
      </article>
    </section>

    <el-alert
      v-if="insecureHttp"
      type="warning"
      :closable="false"
      title="当前使用 HTTP。仅建议在受信任的内网或反向代理后使用；公网 CookieCloud 请优先使用 HTTPS。"
      class="cookiecloud-alert"
    />

    <section class="cookiecloud-card">
      <div class="cookiecloud-card-heading">
        <div class="section-icon"><ShieldCheck :size="22" /></div>
        <div class="section-heading-copy">
          <h3>基础配置</h3>
          <p>配置 CookieCloud 服务的连接信息。</p>
        </div>
        <div class="toggle-control">
          <el-switch v-model="draft.enabled" aria-label="启用 CookieCloud" />
          <div>
            <strong>启用 CookieCloud</strong>
          </div>
        </div>
      </div>

      <el-form label-position="left" label-width="126px" class="cookiecloud-fields">
        <el-form-item label="服务器地址">
          <el-input
            v-model="draft.serverUrl"
            placeholder="https://cookie.example.com 或 https://host/api"
          />
        </el-form-item>
        <el-form-item label="UUID">
          <el-input v-model="draft.uuid" autocomplete="off" placeholder="CookieCloud UUID" />
        </el-form-item>
        <el-form-item label="密码">
          <div class="password-field">
            <el-input
              v-model="draft.password"
              type="password"
              show-password
              autocomplete="new-password"
              :placeholder="
                settings?.password_configured ? '已配置；留空则使用已保存密码' : 'CookieCloud 密码'
              "
            />
            <el-checkbox
              v-if="settings?.password_configured"
              v-model="draft.clearPassword"
              :disabled="Boolean(draft.password)"
              >清除已保存密码</el-checkbox
            >
          </div>
        </el-form-item>
      </el-form>
    </section>

    <section class="cookiecloud-card">
      <div class="cookiecloud-card-heading">
        <div class="section-icon"><Clock3 :size="22" /></div>
        <div class="section-heading-copy">
          <h3>自动同步设置</h3>
          <p>设置 CookieCloud 的自动同步计划。</p>
        </div>
        <div class="toggle-control">
          <el-switch v-model="draft.autoSync" aria-label="自动同步" />
          <div><strong>自动同步</strong><small>启用后将按计划自动执行同步任务</small></div>
        </div>
      </div>

      <el-form
        label-position="left"
        label-width="126px"
        class="cookiecloud-fields cookiecloud-schedule-fields"
      >
        <el-form-item label="请求超时（秒）">
          <el-input-number v-model="draft.requestTimeoutSeconds" :min="1" :max="120" />
        </el-form-item>
        <el-form-item v-if="draft.autoSync" label="Cron 表达式">
          <div class="cron-editor">
            <el-input v-model="draft.syncCronExpression" placeholder="*/30 * * * *" />
            <div class="cookiecloud-cron-presets" aria-label="Cron 快捷设置">
              <el-button @click="applyCronPreset('*/5 * * * *')">每 5 分钟</el-button>
              <el-button @click="applyCronPreset('*/10 * * * *')">每 10 分钟</el-button>
              <el-button @click="applyCronPreset('0 * * * *')">每小时</el-button>
              <el-button @click="applyCronPreset('0 3 * * *')">每天 03:00</el-button>
              <el-button @click="applyCronPreset('0 3 * * 1')">每周一 03:00</el-button>
            </div>
          </div>
        </el-form-item>
      </el-form>
    </section>

    <section class="cookiecloud-runs-card">
      <div class="cookiecloud-runs-heading">
        <div class="section-icon"><CalendarDays :size="22" /></div>
        <div class="section-heading-copy">
          <h3>未来 5 次执行时间</h3>
          <p>按照当前 Cron 配置预览自动同步时间。</p>
        </div>
        <span class="timezone-label">时区：{{ cronPreview?.timezone || 'Asia/Shanghai' }}</span>
      </div>
      <div v-if="cronPreviewLoading" class="cookiecloud-runs-message">正在计算未来执行时间…</div>
      <div v-else-if="cronPreviewError" class="cookiecloud-runs-message field-error">
        {{ cronPreviewError }}
      </div>
      <div v-else-if="!draft.autoSync" class="cookiecloud-runs-message">
        自动同步已关闭，开启后可预览未来 5 次执行时间。
      </div>
      <div v-else-if="cronPreview" class="cookiecloud-runs-grid">
        <article
          v-for="(run, index) in cronPreview.next_runs"
          :key="run"
          class="cookiecloud-run-item"
        >
          <span class="run-index">第 {{ index + 1 }} 次</span>
          <CalendarDays :size="23" class="run-calendar-icon" aria-hidden="true" />
          <strong class="run-date">{{ formatRunDate(run) }}</strong>
          <strong class="run-time">{{ formatRunTime(run) }}</strong>
          <small>{{ cronPreview?.timezone || 'Asia/Shanghai' }}</small>
        </article>
      </div>
      <div v-else class="cookiecloud-runs-message">
        输入有效的 Cron 表达式后，将在此显示未来 5 次执行时间。
      </div>
    </section>

    <footer class="cookiecloud-actions">
      <el-button
        :loading="probing"
        :disabled="
          !draft.serverUrl.trim() ||
          !draft.uuid.trim() ||
          (!draft.password && (!settings?.password_configured || draft.clearPassword))
        "
        @click="probe"
      >
        <ShieldCheck :size="16" /> 测试连接
      </el-button>
      <el-button
        :disabled="!settings?.password_configured || syncing"
        :aria-busy="syncing"
        @click="syncNow"
      >
        <RefreshCw
          :size="16"
          class="sync-button-icon"
          :class="{ 'sync-button-icon--spinning': syncing }"
        />
        立即同步
      </el-button>
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
    </footer>
  </div>
</template>

<style scoped>
.sync-button-icon {
  display: inline-block;
  flex: 0 0 auto;
  transform-origin: 50% 50%;
}

.sync-button-icon--spinning {
  animation: cookiecloud-sync-spin 0.9s linear infinite;
}

@keyframes cookiecloud-sync-spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.cookiecloud-management {
  display: grid;
  gap: 16px;
  min-width: 0;
}

.cookiecloud-page-heading {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 2px 2px 4px;
}

.cookiecloud-title-icon {
  display: grid;
  flex: 0 0 48px;
  width: 48px;
  height: 48px;
  place-items: center;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  border-radius: 13px;
}

.cookiecloud-page-heading h2,
.cookiecloud-card-heading h3,
.cookiecloud-runs-heading h3 {
  margin: 0;
  color: var(--el-text-color-primary);
  font-size: 20px;
  font-weight: 700;
}

.cookiecloud-page-heading p,
.cookiecloud-card-heading p,
.cookiecloud-runs-heading p {
  margin: 5px 0 0;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
}

.cookiecloud-status-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.cookiecloud-status-card {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
  min-height: 104px;
  padding: 16px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 12px;
}

.status-icon,
.section-icon {
  display: grid;
  flex: 0 0 44px;
  width: 44px;
  height: 44px;
  place-items: center;
  border-radius: 50%;
}

.status-icon-success {
  color: var(--el-color-success);
  background: var(--el-color-success-light-9);
}

.status-icon-primary,
.section-icon {
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.status-copy {
  display: grid;
  flex: 1;
  min-width: 0;
  gap: 6px;
}

.status-copy > span {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.status-copy > strong {
  overflow-wrap: anywhere;
  font-size: 17px;
}

.status-copy small {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.4;
}

.status-text-success {
  color: var(--el-color-success);
}
.status-text-danger {
  color: var(--el-color-danger);
}
.status-text-info {
  color: var(--el-text-color-primary);
}

.cookiecloud-result-metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.cookiecloud-result-metrics > div {
  display: grid;
  gap: 3px;
  text-align: center;
}

.cookiecloud-result-metrics > div + div {
  border-left: 1px solid var(--el-border-color-lighter);
}

.cookiecloud-result-metrics strong {
  color: var(--el-color-primary);
  font-size: 22px;
}

.cookiecloud-result-metrics .result-success {
  color: var(--el-color-success);
}
.cookiecloud-result-metrics .result-failed {
  color: var(--el-color-danger);
}

.cookiecloud-card,
.cookiecloud-runs-card {
  display: grid;
  gap: 16px;
  min-width: 0;
  padding: 20px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 12px;
}

.cookiecloud-card-heading,
.cookiecloud-runs-heading {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.cookiecloud-card-heading .section-icon,
.cookiecloud-runs-heading .section-icon {
  flex-basis: 40px;
  width: 40px;
  height: 40px;
}

.section-heading-copy {
  flex: 1;
  min-width: 0;
}

.section-heading-copy h3,
.cookiecloud-runs-heading h3 {
  font-size: 18px;
}

.section-heading-copy p,
.cookiecloud-runs-heading p {
  font-size: 13px;
}

.toggle-control {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-left: auto;
}

.toggle-control > div {
  display: grid;
  gap: 3px;
}

.toggle-control strong {
  font-size: 13px;
}
.toggle-control small {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.cookiecloud-fields {
  display: grid;
  gap: 4px;
}

.cookiecloud-fields :deep(.el-form-item) {
  display: grid;
  grid-template-columns: 126px minmax(0, 1fr);
  align-items: center;
  margin-bottom: 8px;
}

.cookiecloud-fields :deep(.el-form-item__label) {
  align-items: center;
  justify-content: flex-end;
  height: 100%;
  padding: 0 18px 0 0;
  color: var(--el-text-color-primary);
}

.cookiecloud-fields :deep(.el-form-item__content) {
  min-width: 0;
}
.cookiecloud-fields :deep(.el-input-number) {
  width: 180px;
}

.password-field {
  display: grid;
  gap: 8px;
  width: 100%;
}

.cookiecloud-alert {
  margin: 0;
}

.cron-editor {
  display: grid;
  width: 100%;
  gap: 10px;
}

.cookiecloud-cron-presets {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.cookiecloud-cron-presets .el-button {
  margin-left: 0;
}

.cookiecloud-runs-card {
  gap: 18px;
  padding: 20px;
  background: linear-gradient(135deg, var(--el-color-primary-light-9), var(--el-bg-color) 70%);
  border-color: var(--el-color-primary-light-7);
}

.timezone-label {
  margin-left: auto;
  color: var(--el-color-primary);
  font-size: 12px;
  white-space: nowrap;
}

.cookiecloud-runs-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 10px;
}

.cookiecloud-run-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  min-width: 0;
  padding: 14px 8px;
  text-align: center;
  background: var(--el-bg-color);
  border: 1px solid var(--el-color-primary-light-7);
  border-radius: 10px;
}

.run-index {
  padding: 3px 10px;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
}

.run-calendar-icon,
.run-time {
  color: var(--el-color-primary);
}
.run-date {
  font-size: 14px;
}
.run-time {
  font-size: 18px;
}
.cookiecloud-run-item small {
  color: var(--el-text-color-secondary);
  font-size: 11px;
}

.cookiecloud-runs-message {
  padding: 18px;
  color: var(--el-text-color-secondary);
  text-align: center;
  background: var(--el-bg-color);
  border: 1px dashed var(--el-border-color);
  border-radius: 8px;
}

.field-error {
  color: var(--el-color-danger);
}

.cookiecloud-actions {
  position: sticky;
  bottom: 0;
  z-index: 2;
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 12px 0;
  background: var(--el-bg-color);
  border-top: 1px solid var(--el-border-color-lighter);
}

@media (max-width: 1200px) {
  .cookiecloud-status-grid {
    grid-template-columns: 1fr 1fr;
  }
  .cookiecloud-result-card {
    grid-column: 1 / -1;
  }
  .cookiecloud-runs-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {
  .cookiecloud-status-grid,
  .cookiecloud-runs-grid {
    grid-template-columns: 1fr;
  }
  .cookiecloud-result-card {
    grid-column: auto;
  }
  .cookiecloud-card,
  .cookiecloud-runs-card {
    padding: 14px;
  }
  .cookiecloud-card-heading,
  .cookiecloud-runs-heading {
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .toggle-control {
    width: 100%;
    margin-left: 52px;
  }
  .cookiecloud-fields :deep(.el-form-item) {
    grid-template-columns: 1fr;
    gap: 4px;
  }
  .cookiecloud-fields :deep(.el-form-item__label) {
    justify-content: flex-start;
    padding: 0;
  }
  .timezone-label {
    width: 100%;
    margin-left: 52px;
  }
  .cookiecloud-actions {
    flex-wrap: wrap;
  }
  .cookiecloud-actions .el-button {
    flex: 1;
  }
}
</style>
