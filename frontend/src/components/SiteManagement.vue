<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { storeToRefs } from 'pinia';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Activity, Globe, Info, Plus, RefreshCw, Settings2 } from '@lucide/vue';

import { ApiProblem, toApiProblem } from '../api/client';
import type {
  Site,
  SiteCreateInput,
  SiteCredentialInput,
  SiteHealth,
  SiteKind,
  SitePatchInput,
  SiteProfile,
  SiteTemporaryProbeInput,
  SiteUserProfile,
} from '../api/sites';
import { useSiteStore } from '../stores/sites';

interface SiteDraft {
  id: string | null;
  originalType: SiteKind | null;
  name: string;
  type: SiteKind;
  credential: string;
  clearCredential: boolean;
  credentialConfigured: boolean;
  requestTimeoutSeconds: number;
  searchIntervalSeconds: number;
  userAgent: string;
  browserEmulationEnabled: boolean;
  proxyEnabled: boolean;
  proxyHost: string;
  proxyPort: number | null;
  proxyUsername: string;
  proxyPassword: string;
  clearProxyPassword: boolean;
  proxyCredentialConfigured: boolean;
  enableAfterSave: boolean;
}

const store = useSiteStore();
const { items, profiles, health, userProfiles, userProfileErrors, loading, error, busy } =
  storeToRefs(store);
const dialog = ref(false);
const detailDialog = ref(false);
const detailSiteId = ref<string | null>(null);
const saving = ref(false);
const probingDraft = ref(false);
const draft = reactive<SiteDraft>({
  id: null,
  originalType: null,
  name: '',
  type: 'MTEAM',
  credential: '',
  clearCredential: false,
  credentialConfigured: false,
  requestTimeoutSeconds: 15,
  searchIntervalSeconds: 0,
  userAgent: '',
  browserEmulationEnabled: false,
  proxyEnabled: false,
  proxyHost: '',
  proxyPort: null,
  proxyUsername: '',
  proxyPassword: '',
  clearProxyPassword: false,
  proxyCredentialConfigured: false,
  enableAfterSave: false,
});

const editing = computed(() => draft.id !== null);
const selectedProfile = computed<SiteProfile | undefined>(() => profileFor(draft.type));
const detailSite = computed<Site | undefined>(() =>
  detailSiteId.value ? items.value.find((item) => item.id === detailSiteId.value) : undefined,
);
const detailProfile = computed<SiteUserProfile | undefined>(() =>
  detailSiteId.value ? userProfiles.value[detailSiteId.value] : undefined,
);
const detailError = computed<ApiProblem | undefined>(() =>
  detailSiteId.value ? userProfileErrors.value[detailSiteId.value] : undefined,
);

onMounted(() => {
  void refresh(false);
});

function profileFor(kind: SiteKind): SiteProfile | undefined {
  return profiles.value.find((profile) => profile.kind === kind);
}

function kindLabel(kind: SiteKind): string {
  return profileFor(kind)?.display_name ?? kind;
}

function credentialLabel(kind: SiteKind): string {
  const credentialKind = profileFor(kind)?.credential_kind;
  if (credentialKind === 'API_KEY') return 'API Key';
  return 'Cookie';
}

function supportLabel(profile: SiteProfile | undefined): string {
  if (!profile) return '配置未加载';
  if (profile.support_status === 'SUPPORTED') return '已完成适配';
  if (profile.support_status === 'PENDING_REAL_VALIDATION') return '待真实验收';
  return '待适配';
}

function connectionLabel(status: Site['connection_status']): string {
  if (status === 'OK') return '已验证';
  if (status === 'FAILED') return '失败';
  return '未测试';
}

function statusTag(status: Site['connection_status']): 'success' | 'danger' | 'info' {
  if (status === 'OK') return 'success';
  if (status === 'FAILED') return 'danger';
  return 'info';
}

function statusState(item: Site): 'ok' | 'degraded' | 'failed' | 'idle' {
  if (!item.enabled || item.connection_status === 'UNTESTED') return 'idle';
  const siteHealth = health.value[item.id];
  if (item.connection_status === 'FAILED' || siteHealth?.circuit_state === 'OPEN') return 'failed';
  if (
    siteHealth?.circuit_state === 'HALF_OPEN' ||
    (siteHealth?.rate_limit_wait_seconds ?? 0) > 0 ||
    siteHealth?.last_error_code === 'SITE_RATE_LIMITED' ||
    siteHealth?.last_error_code === 'SITE_UNAVAILABLE'
  ) {
    return 'degraded';
  }
  return 'ok';
}

function statusText(item: Site): string {
  if (!item.enabled) return '已停用';
  if (item.connection_status === 'UNTESTED') return '未测试';
  if (statusState(item) === 'failed') return '连接失败或熔断';
  if (statusState(item) === 'degraded') return '暂时降级';
  return '连接正常';
}

function circuitLabel(value: SiteHealth['circuit_state'] | undefined): string {
  if (value === 'OPEN') return '已打开';
  if (value === 'HALF_OPEN') return '半开探测';
  if (value === 'CLOSED') return '关闭';
  return '未知';
}

function circuitTag(
  value: SiteHealth['circuit_state'] | undefined,
): 'success' | 'danger' | 'warning' | 'info' {
  if (value === 'OPEN') return 'danger';
  if (value === 'HALF_OPEN') return 'warning';
  if (value === 'CLOSED') return 'success';
  return 'info';
}

function isBusy(item: Site, operation: string): boolean {
  return busy.value[`${operation}:${item.id}`] === true;
}

function problemText(problem: ApiProblem): string {
  return problem.traceId ? `${problem.message} · trace_id ${problem.traceId}` : problem.message;
}

function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return '--';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  return `${amount.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${units[unit]}`;
}

function formatNumber(value: number | null | undefined): string {
  return value === null || value === undefined ? '--' : value.toLocaleString();
}

function formatDecimal(value: number | null | undefined): string {
  return value === null || value === undefined
    ? '--'
    : value.toLocaleString(undefined, { maximumFractionDigits: 3 });
}

async function refresh(notify = true) {
  try {
    await store.refresh();
    if (notify) ElMessage.success('站点配置与可靠性状态已刷新');
  } catch (caught) {
    if (notify) ElMessage.error(problemText(toApiProblem(caught)));
  }
}

function resetDraft() {
  Object.assign(draft, {
    id: null,
    originalType: null,
    name: '',
    type: 'MTEAM' as SiteKind,
    credential: '',
    clearCredential: false,
    credentialConfigured: false,
    requestTimeoutSeconds: 15,
    searchIntervalSeconds: 0,
    userAgent: '',
    browserEmulationEnabled: false,
    proxyEnabled: false,
    proxyHost: '',
    proxyPort: null,
    proxyUsername: '',
    proxyPassword: '',
    clearProxyPassword: false,
    proxyCredentialConfigured: false,
    enableAfterSave: false,
  });
}

function applyProfileDefaults() {
  const profile = selectedProfile.value;
  if (!profile) return;
  draft.requestTimeoutSeconds = profile.request_timeout_seconds;
  draft.searchIntervalSeconds = Math.round(profile.search_interval_seconds);
  draft.userAgent = '';
  draft.browserEmulationEnabled = false;
  if (!profile.supports_proxy) draft.proxyEnabled = false;
}

function openCreate() {
  resetDraft();
  applyProfileDefaults();
  dialog.value = true;
}

function openEdit(item: Site) {
  resetDraft();
  Object.assign(draft, {
    id: item.id,
    originalType: item.type,
    name: item.name,
    type: item.type,
    credentialConfigured: item.credential_configured,
    requestTimeoutSeconds: item.request_timeout_seconds,
    searchIntervalSeconds: item.search_interval_seconds,
    userAgent: item.user_agent ?? '',
    browserEmulationEnabled: item.browser_emulation_enabled,
    proxyEnabled: item.proxy_enabled,
    proxyHost: item.proxy_host ?? '',
    proxyPort: item.proxy_port,
    proxyUsername: item.proxy_username ?? '',
    proxyCredentialConfigured: item.proxy_credential_configured,
  });
  dialog.value = true;
}

function credentialPayload(): SiteCredentialInput | undefined {
  if (draft.clearCredential || !draft.credential) return undefined;
  const profile = selectedProfile.value;
  if (!profile) return undefined;
  return {
    kind: profile.credential_kind,
    value: draft.credential,
  };
}

function validateDraft(requireCredential = false): boolean {
  if (selectedProfile.value?.support_status !== 'SUPPORTED') {
    ElMessage.warning('该站点已进入 Registry，但适配器尚未开放，当前版本不能保存或测试');
    return false;
  }
  if (!draft.name.trim()) {
    ElMessage.warning('请输入站点名称');
    return false;
  }
  if (!Number.isInteger(draft.requestTimeoutSeconds) || draft.requestTimeoutSeconds < 1) {
    ElMessage.warning('请求超时必须是大于 0 的整数秒');
    return false;
  }
  if (!Number.isInteger(draft.searchIntervalSeconds) || draft.searchIntervalSeconds < 0) {
    ElMessage.warning('搜索间隔必须是非负整数秒');
    return false;
  }
  if (/\r|\n|\0/.test(draft.credential) || /\r|\n|\0/.test(draft.userAgent)) {
    ElMessage.warning('凭证和 User-Agent 不能包含换行或 NUL');
    return false;
  }
  if (requireCredential && !credentialPayload()) {
    ElMessage.warning('测试未保存配置前需要填写站点凭证');
    return false;
  }
  if (draft.proxyEnabled && (!draft.proxyHost.trim() || draft.proxyPort === null)) {
    ElMessage.warning('启用代理时必须填写代理地址和端口');
    return false;
  }
  if (draft.proxyPassword && !draft.proxyUsername.trim()) {
    ElMessage.warning('配置代理密码时必须填写代理账号');
    return false;
  }
  return true;
}

function proxyCreatePayload() {
  return {
    enabled: draft.proxyEnabled,
    ...(draft.proxyHost.trim() ? { host: draft.proxyHost.trim() } : {}),
    ...(draft.proxyPort !== null ? { port: draft.proxyPort } : {}),
    ...(draft.proxyUsername.trim() ? { username: draft.proxyUsername.trim() } : {}),
    ...(draft.proxyPassword ? { password: draft.proxyPassword } : {}),
  };
}

function temporaryProbePayload(): SiteTemporaryProbeInput | null {
  const credential = credentialPayload();
  if (!credential) return null;
  return {
    type: draft.type,
    credential,
    request_timeout_seconds: draft.requestTimeoutSeconds,
    search_interval_seconds: draft.searchIntervalSeconds,
    user_agent: draft.userAgent.trim() || null,
    browser_emulation_enabled: draft.browserEmulationEnabled,
    proxy: proxyCreatePayload(),
  };
}

async function testDraftConnection() {
  if (!validateDraft(true)) return;
  const payload = temporaryProbePayload();
  if (!payload) return;
  probingDraft.value = true;
  try {
    await store.probeTemporary(payload);
    ElMessage.success('当前表单只读连接测试通过；配置尚未保存');
  } catch (caught) {
    await handleWriteProblem(caught);
  } finally {
    probingDraft.value = false;
  }
}

async function handleWriteProblem(caught: unknown) {
  const problem = toApiProblem(caught);
  if (problem.status === 412) {
    await store.refresh().catch(() => undefined);
    ElMessage.warning('站点配置已被其他请求更新，已刷新，请重新打开后再修改');
    return;
  }
  if (problem.status === 401) {
    ElMessage.error('管理会话已失效，请重新登录 PackBreaker');
    return;
  }
  ElMessage.error(problemText(problem));
}

function runtimeChanged(current: Site): boolean {
  return (
    current.request_timeout_seconds !== draft.requestTimeoutSeconds ||
    current.search_interval_seconds !== draft.searchIntervalSeconds ||
    (current.user_agent ?? '') !== draft.userAgent.trim() ||
    current.browser_emulation_enabled !== draft.browserEmulationEnabled ||
    current.proxy_enabled !== draft.proxyEnabled ||
    (current.proxy_host ?? '') !== draft.proxyHost.trim() ||
    current.proxy_port !== draft.proxyPort ||
    (current.proxy_username ?? '') !== draft.proxyUsername.trim()
  );
}

async function save() {
  if (!validateDraft()) return;
  const credential = credentialPayload();
  const current = draft.id ? items.value.find((item) => item.id === draft.id) : undefined;
  if (
    current &&
    draft.type !== draft.originalType &&
    current.credential_configured &&
    !credential &&
    !draft.clearCredential
  ) {
    ElMessage.warning('切换站点类型时必须同时替换或清除已有凭证');
    return;
  }

  saving.value = true;
  try {
    if (current) {
      const patch: SitePatchInput = { clear_credential: false };
      if (draft.name.trim() !== current.name) patch.name = draft.name.trim();
      if (draft.type !== current.type) {
        patch.type = draft.type;
      }
      if (draft.clearCredential) {
        patch.clear_credential = true;
      } else if (credential) {
        patch.credential = credential;
      }
      if (runtimeChanged(current) || draft.type !== current.type) {
        patch.request_timeout_seconds = draft.requestTimeoutSeconds;
        patch.search_interval_seconds = draft.searchIntervalSeconds;
        patch.user_agent = draft.userAgent.trim() || null;
        patch.browser_emulation_enabled = draft.browserEmulationEnabled;
        patch.proxy = {
          clear_password: draft.clearProxyPassword,
          enabled: draft.proxyEnabled,
          host: draft.proxyHost.trim() || null,
          port: draft.proxyPort,
          username: draft.proxyUsername.trim() || null,
          ...(!draft.clearProxyPassword && draft.proxyPassword
            ? { password: draft.proxyPassword }
            : {}),
        };
      } else if (draft.proxyPassword || draft.clearProxyPassword) {
        patch.proxy = {
          clear_password: draft.clearProxyPassword,
          ...(!draft.clearProxyPassword && draft.proxyPassword
            ? { password: draft.proxyPassword }
            : {}),
        };
      }
      if (!Object.keys(patch).length) {
        dialog.value = false;
        ElMessage.info('配置没有变化');
        return;
      }
      await store.update(current, patch);
      ElMessage.success('站点配置已保存；连接相关变更会自动停用并要求重新测试');
    } else {
      const payload: SiteCreateInput = {
        name: draft.name.trim(),
        type: draft.type,
        ...(credential ? { credential } : {}),
        request_timeout_seconds: draft.requestTimeoutSeconds,
        search_interval_seconds: draft.searchIntervalSeconds,
        user_agent: draft.userAgent.trim() || null,
        browser_emulation_enabled: draft.browserEmulationEnabled,
        proxy: proxyCreatePayload(),
      };
      const created = await store.create(payload);
      if (draft.enableAfterSave) {
        try {
          await store.testConnection(created);
          const latest = items.value.find((item) => item.id === created.id);
          if (latest) await store.setEnabled(latest, true);
          ElMessage.success('站点已保存、连接测试通过并启用');
        } catch (caught) {
          dialog.value = false;
          await refresh(false);
          ElMessage.warning(
            `站点已保存，但自动测试/启用失败：${problemText(toApiProblem(caught))}`,
          );
          return;
        }
      } else {
        ElMessage.success('站点配置已创建，默认保持停用');
      }
    }
    dialog.value = false;
  } catch (caught) {
    await handleWriteProblem(caught);
  } finally {
    saving.value = false;
  }
}

async function testConnection(item: Site) {
  try {
    await store.testConnection(item);
    ElMessage.success(`${item.name} 只读连接测试通过`);
  } catch (caught) {
    await handleWriteProblem(caught);
  }
}

async function loadDetails(item: Site, notifyError = true) {
  try {
    await store.loadUserProfile(item);
  } catch (caught) {
    if (notifyError) ElMessage.error(problemText(toApiProblem(caught)));
  }
}

function openDetails(item: Site) {
  detailSiteId.value = item.id;
  detailDialog.value = true;
  void loadDetails(item, false);
}

async function refreshDetails() {
  if (!detailSite.value) return;
  await loadDetails(detailSite.value);
}

async function toggleEnabled(item: Site, enabled: boolean) {
  if (item.enabled === enabled) return;
  try {
    await store.setEnabled(item, enabled);
    ElMessage.success(`${item.name} 已${enabled ? '启用' : '停用'}`);
  } catch (caught) {
    await handleWriteProblem(caught);
  }
}

async function resetCircuit(item: Site) {
  try {
    await ElMessageBox.confirm(
      `重置「${item.name}」当前配置版本的熔断状态？此动作只清除进程内熔断失败计数，不会测试连接，也不会把站点标记为恢复。`,
      '确认重置站点熔断器',
      { confirmButtonText: '仅重置熔断器', cancelButtonText: '取消', type: 'warning' },
    );
  } catch {
    return;
  }
  try {
    await store.resetCircuit(item);
    ElMessage.success('熔断器已重置；站点是否恢复以之后的请求或连接测试为准');
  } catch (caught) {
    await handleWriteProblem(caught);
  }
}

async function remove(item: Site) {
  try {
    await ElMessageBox.confirm(
      `删除站点配置「${item.name}」？只删除 PackBreaker 配置与加密凭证，不会访问或修改远端站点。`,
      '确认删除站点配置',
      { confirmButtonText: '删除配置', cancelButtonText: '取消', type: 'warning' },
    );
  } catch {
    return;
  }
  try {
    await store.remove(item);
    ElMessage.success('站点配置已删除');
  } catch (caught) {
    await handleWriteProblem(caught);
  }
}
</script>

<template>
  <div>
    <div class="site-toolbar">
      <el-button :loading="loading" @click="refresh()"><RefreshCw :size="15" />刷新</el-button>
      <el-button type="primary" @click="openCreate"><Plus :size="15" />添加站点</el-button>
    </div>

    <el-alert
      v-if="error"
      :title="error.status === 401 ? '需要管理员登录' : '站点配置暂不可用'"
      :description="problemText(error)"
      type="warning"
      :closable="false"
      show-icon
      class="section-space"
    />

    <div v-loading="loading" class="connection-cards">
      <article v-for="item in items" :key="item.id" class="panel connection-card">
        <div class="card-title">
          <span class="connection-icon"><Globe :size="25" /></span>
          <div>
            <h3 class="site-name-line">
              <span
                class="site-status-dot"
                :class="`site-status-dot--${statusState(item)}`"
                :title="statusText(item)"
              />
              {{ item.name }}
            </h3>
            <small>{{ kindLabel(item.type) }} · 配置 v{{ item.version }}</small>
          </div>
          <el-switch
            :model-value="item.enabled"
            :loading="isBusy(item, 'enable')"
            :aria-label="'启用' + item.name"
            @change="toggleEnabled(item, $event === true)"
          />
        </div>
        <p class="endpoint">{{ item.base_url }}</p>
        <div class="downloader-probe-tags">
          <el-tag :type="statusTag(item.connection_status)">
            连接 {{ connectionLabel(item.connection_status) }}
          </el-tag>
          <el-tag :type="item.credential_configured ? 'success' : 'info'">
            {{ item.credential_configured ? `${credentialLabel(item.type)} 已配置` : '未配置凭证' }}
          </el-tag>
          <el-tag :type="circuitTag(health[item.id]?.circuit_state)">
            熔断 {{ circuitLabel(health[item.id]?.circuit_state) }}
          </el-tag>
          <el-tag type="info">{{ supportLabel(profileFor(item.type)) }}</el-tag>
        </div>
        <dl class="config-summary">
          <dt>请求超时</dt>
          <dd>{{ item.request_timeout_seconds }} 秒</dd>
          <dt>搜索间隔</dt>
          <dd>{{ item.search_interval_seconds }} 秒</dd>
          <dt>代理</dt>
          <dd>
            {{
              item.proxy_enabled ? `${item.proxy_host ?? '—'}:${item.proxy_port ?? '—'}` : '未启用'
            }}
          </dd>
          <dt>最近测试</dt>
          <dd>
            {{ item.last_test_at ? new Date(item.last_test_at).toLocaleString() : '尚未测试' }}
          </dd>
          <dt>可靠性错误</dt>
          <dd>{{ health[item.id]?.last_error_code ?? '—' }}</dd>
        </dl>
        <div class="card-actions">
          <el-button size="small" :loading="isBusy(item, 'profile')" @click="openDetails(item)">
            <Info :size="14" />详情
          </el-button>
          <el-button size="small" :loading="isBusy(item, 'test')" @click="testConnection(item)">
            <Activity :size="14" />测试连接
          </el-button>
          <el-button size="small" @click="openEdit(item)"> <Settings2 :size="14" />配置 </el-button>
          <el-button
            v-if="health[item.id]"
            link
            type="warning"
            :loading="isBusy(item, 'reset')"
            @click="resetCircuit(item)"
            >重置熔断</el-button
          >
          <el-button link type="danger" :loading="isBusy(item, 'delete')" @click="remove(item)">
            删除
          </el-button>
        </div>
      </article>
    </div>

    <el-empty v-if="!loading && !items.length && !error" description="暂无站点配置">
      <el-button type="primary" @click="openCreate">添加第一个站点</el-button>
    </el-empty>

    <el-dialog
      v-model="dialog"
      :title="editing ? '编辑站点' : '添加站点'"
      width="min(680px, 94vw)"
      @closed="resetDraft"
    >
      <el-form label-position="top">
        <div class="form-grid">
          <el-form-item label="名称" required>
            <el-input v-model="draft.name" maxlength="80" />
          </el-form-item>
          <el-form-item label="站点类型" required>
            <el-select v-model="draft.type" @change="applyProfileDefaults">
              <el-option
                v-for="profile in profiles"
                :key="profile.kind"
                :label="`${profile.display_name} · ${supportLabel(profile)}`"
                :value="profile.kind"
                :disabled="profile.support_status !== 'SUPPORTED'"
              />
            </el-select>
          </el-form-item>
        </div>

        <el-form-item label="站点地址（由 Profile 固定）">
          <el-input :model-value="selectedProfile?.base_url ?? ''" disabled />
        </el-form-item>

        <el-alert
          v-if="editing && draft.credentialConfigured"
          title="凭证框留空将保留现有凭证。"
          type="info"
          :closable="false"
          class="form-alert"
        />
        <el-checkbox v-if="editing && draft.credentialConfigured" v-model="draft.clearCredential">
          清除现有凭证并保持站点停用
        </el-checkbox>

        <el-form-item v-if="!draft.clearCredential" :label="credentialLabel(draft.type)">
          <el-input
            v-model="draft.credential"
            type="password"
            show-password
            autocomplete="new-password"
            :placeholder="editing && draft.credentialConfigured ? '留空保持原凭证' : '请输入凭证'"
          />
        </el-form-item>

        <div class="form-grid">
          <el-form-item label="请求超时（秒）">
            <el-input-number v-model="draft.requestTimeoutSeconds" :min="1" :max="120" />
          </el-form-item>
          <el-form-item label="搜索间隔（秒）">
            <el-input-number v-model="draft.searchIntervalSeconds" :min="0" :max="3600" />
          </el-form-item>
        </div>

        <template v-if="selectedProfile?.supports_user_agent">
          <el-form-item label="User-Agent">
            <el-input
              v-model="draft.userAgent"
              maxlength="512"
              placeholder="留空使用系统默认浏览器 User-Agent"
            />
          </el-form-item>
          <el-form-item v-if="selectedProfile.supports_browser_emulation">
            <el-checkbox v-model="draft.browserEmulationEnabled">启用浏览器请求头仿真</el-checkbox>
          </el-form-item>
        </template>

        <template v-if="selectedProfile?.supports_proxy">
          <el-divider content-position="left">独立代理</el-divider>
          <el-form-item>
            <el-switch v-model="draft.proxyEnabled" active-text="启用该站点独立代理" />
          </el-form-item>
          <div v-if="draft.proxyEnabled" class="form-grid">
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
                :disabled="draft.clearProxyPassword"
                type="password"
                show-password
                autocomplete="new-password"
                :placeholder="draft.proxyCredentialConfigured ? '留空保持现有代理密码' : '可选'"
              />
            </el-form-item>
          </div>
          <el-checkbox
            v-if="editing && draft.proxyCredentialConfigured"
            v-model="draft.clearProxyPassword"
            >清除已保存的代理密码</el-checkbox
          >
        </template>

        <el-form-item v-if="!editing">
          <el-checkbox v-model="draft.enableAfterSave">保存后自动测试连接并启用</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button
          v-if="!editing || draft.credential"
          :loading="probingDraft"
          :disabled="selectedProfile?.support_status !== 'SUPPORTED'"
          @click="testDraftConnection"
          >测试当前表单</el-button
        >
        <el-button @click="dialog = false">取消</el-button>
        <el-button
          type="primary"
          :loading="saving"
          :disabled="selectedProfile?.support_status !== 'SUPPORTED'"
          @click="save"
          >保存配置</el-button
        >
      </template>
    </el-dialog>

    <el-dialog
      v-model="detailDialog"
      :title="detailSite ? `${detailSite.name} · 用户详情` : '站点用户详情'"
      width="min(820px, 94vw)"
      @closed="detailSiteId = null"
    >
      <el-alert
        v-if="detailError"
        title="站点用户详情暂不可用"
        :description="problemText(detailError)"
        type="warning"
        :closable="false"
        show-icon
        class="form-alert"
      />
      <el-skeleton
        v-if="detailSite && isBusy(detailSite, 'profile') && !detailProfile"
        :rows="6"
        animated
      />
      <el-descriptions v-else-if="detailProfile" :column="2" border>
        <el-descriptions-item label="UID">{{ detailProfile.uid ?? '--' }}</el-descriptions-item>
        <el-descriptions-item label="用户名">{{
          detailProfile.username ?? '--'
        }}</el-descriptions-item>
        <el-descriptions-item label="用户等级">{{
          detailProfile.user_level ?? '--'
        }}</el-descriptions-item>
        <el-descriptions-item label="分享率">{{
          formatDecimal(detailProfile.ratio)
        }}</el-descriptions-item>
        <el-descriptions-item label="真实上传量">{{
          formatBytes(detailProfile.real_uploaded_bytes)
        }}</el-descriptions-item>
        <el-descriptions-item label="真实下载量">{{
          formatBytes(detailProfile.real_downloaded_bytes)
        }}</el-descriptions-item>
        <el-descriptions-item label="上传量">{{
          formatBytes(detailProfile.uploaded_bytes)
        }}</el-descriptions-item>
        <el-descriptions-item label="下载量">{{
          formatBytes(detailProfile.downloaded_bytes)
        }}</el-descriptions-item>
        <el-descriptions-item label="发种数">{{
          formatNumber(detailProfile.torrents_posted)
        }}</el-descriptions-item>
        <el-descriptions-item label="做种数">{{
          formatNumber(detailProfile.seeding_count)
        }}</el-descriptions-item>
        <el-descriptions-item label="做种量">{{
          formatBytes(detailProfile.seeding_size_bytes)
        }}</el-descriptions-item>
        <el-descriptions-item label="魔力值">{{
          formatDecimal(detailProfile.bonus)
        }}</el-descriptions-item>
        <el-descriptions-item label="做种积分">{{
          formatDecimal(detailProfile.seeding_points)
        }}</el-descriptions-item>
        <el-descriptions-item label="每小时魔力值">{{
          formatDecimal(detailProfile.bonus_per_hour)
        }}</el-descriptions-item>
        <el-descriptions-item label="数据更新时间" :span="2">
          {{ new Date(detailProfile.fetched_at).toLocaleString() }}
        </el-descriptions-item>
      </el-descriptions>
      <el-empty v-else-if="!detailError" description="暂无用户详情" />
      <template #footer>
        <el-button
          v-if="detailSite"
          :loading="isBusy(detailSite, 'profile')"
          @click="refreshDetails"
          ><RefreshCw :size="14" />刷新详情</el-button
        >
        <el-button @click="detailDialog = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.site-toolbar {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-bottom: 16px;
}

.site-name-line {
  display: flex;
  align-items: center;
  gap: 8px;
}

.site-status-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  flex: 0 0 9px;
  background: #909399;
}

.site-status-dot--ok {
  background: #67c23a;
}

.site-status-dot--degraded {
  background: #e6a23c;
}

.site-status-dot--failed {
  background: #f56c6c;
}
</style>
