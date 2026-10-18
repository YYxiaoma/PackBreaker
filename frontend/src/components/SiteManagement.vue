<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { storeToRefs } from 'pinia';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Activity, Globe, LayoutGrid, List, Plus, RefreshCw, Search, Settings2 } from '@lucide/vue';

import { ApiProblem, toApiProblem } from '../api/client';
import type {
  Site,
  SiteCreateInput,
  SiteCredentialInput,
  SiteKind,
  SitePatchInput,
  SiteProfile,
  SiteTemporaryProbeInput,
} from '../api/sites';
import { useSiteStore } from '../stores/sites';
import { siteProfileOptionLabel } from '../api/sites';
import SiteDetailDrawer from './SiteDetailDrawer.vue';

interface SiteDraft {
  id: string | null;
  originalType: SiteKind | null;
  name: string;
  type: SiteKind;
  credential: string;
  clearCredential: boolean;
  credentialConfigured: boolean;
  downloadCookie: string;
  clearDownloadCookie: boolean;
  downloadCredentialConfigured: boolean;
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
const { items, profiles, loading, error, busy } = storeToRefs(store);
const dialog = ref(false);
const detailDrawer = ref(false);
const selectedSiteId = ref<string | null>(null);
const searchKeyword = ref('');
const typeFilter = ref<SiteKind | ''>('');
const statusFilter = ref<Site['connection_status'] | ''>('');
const sortMode = ref<'name' | 'status' | 'sync'>('name');
const viewMode = ref<'grid' | 'list'>('grid');

const visibleItems = computed(() => {
  const keyword = searchKeyword.value.trim().toLocaleLowerCase();
  const filtered = items.value.filter((item) => {
    const matchesKeyword =
      !keyword ||
      [item.name, item.base_url, item.user_summary?.username]
        .filter(Boolean)
        .some((value) => String(value).toLocaleLowerCase().includes(keyword));
    const matchesType = !typeFilter.value || item.type === typeFilter.value;
    const matchesStatus = !statusFilter.value || item.connection_status === statusFilter.value;
    return matchesKeyword && matchesType && matchesStatus;
  });

  return filtered.slice().sort((a, b) => {
    if (sortMode.value === 'name') return a.name.localeCompare(b.name, 'zh-CN');
    if (sortMode.value === 'status') {
      return (
        statusRank(a.connection_status) - statusRank(b.connection_status) ||
        a.name.localeCompare(b.name, 'zh-CN')
      );
    }
    return syncTimestamp(b) - syncTimestamp(a) || a.name.localeCompare(b.name, 'zh-CN');
  });
});
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
  downloadCookie: '',
  clearDownloadCookie: false,
  downloadCredentialConfigured: false,
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
  enableAfterSave: true,
});

const editing = computed(() => draft.id !== null);
const selectedProfile = computed<SiteProfile | undefined>(() => profileFor(draft.type));

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

function profileConfigurable(profile: SiteProfile | undefined): boolean {
  return Boolean(profile && profile.support_status !== 'PENDING_ADAPTER');
}

function connectionLabel(status: Site['connection_status']): string {
  return statusLabel(status);
}

function statusTag(status: Site['connection_status']): 'success' | 'danger' | 'info' {
  if (status === 'OK') return 'success';
  if (status === 'FAILED') return 'danger';
  return 'info';
}

function statusState(item: Site): 'ok' | 'failed' | 'idle' {
  if (!item.enabled || item.connection_status === 'UNTESTED') return 'idle';
  if (item.connection_status === 'FAILED') return 'failed';
  return 'ok';
}

function statusText(item: Site): string {
  if (!item.enabled) return '已停用';
  if (item.connection_status === 'UNTESTED') return '未测试';
  if (item.connection_status === 'FAILED') return '连接失败';
  return '连接正常';
}

function statusRank(status: Site['connection_status']): number {
  if (status === 'OK') return 0;
  if (status === 'FAILED') return 1;
  return 2;
}

function syncTimestamp(item: Site): number {
  const value = item.user_summary?.fetched_at;
  if (!value) return 0;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function statusLabel(status: Site['connection_status']): string {
  if (status === 'OK') return '在线';
  if (status === 'FAILED') return '异常';
  return '未测试';
}

function typeLabel(kind: SiteKind): string {
  return kindLabel(kind);
}

function faviconUrl(baseUrl: string): string {
  try {
    const url = new URL(baseUrl);
    return url.origin + '/favicon.ico';
  } catch {
    return '';
  }
}

function hideFavicon(event: Event): void {
  (event.currentTarget as HTMLImageElement | null)?.style.setProperty('display', 'none');
}

function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  if (value < 1024) return value + ' B';
  const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
  let size = value;
  let unit = -1;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return size >= 100 ? size.toFixed(0) + ' ' + units[unit] : size.toFixed(2) + ' ' + units[unit];
}

function formatRatio(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toFixed(2);
}

function isBusy(item: Site, operation: string): boolean {
  return busy.value[`${operation}:${item.id}`] === true;
}

function problemText(problem: ApiProblem): string {
  return problem.traceId ? `${problem.message} · trace_id ${problem.traceId}` : problem.message;
}

async function refresh(notify = true) {
  try {
    await store.refresh();
    if (notify) ElMessage.success('站点配置已刷新');
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
    downloadCookie: '',
    clearDownloadCookie: false,
    downloadCredentialConfigured: false,
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
    enableAfterSave: true,
  });
}

function applyProfileDefaults() {
  const profile = selectedProfile.value;
  if (!profile) return;
  draft.requestTimeoutSeconds = profile.request_timeout_seconds;
  draft.searchIntervalSeconds = Math.round(profile.search_interval_seconds);
  draft.userAgent = '';
  draft.browserEmulationEnabled = false;
  draft.downloadCookie = '';
  draft.clearDownloadCookie = false;
  draft.enableAfterSave = profile.support_status === 'SUPPORTED';
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
    downloadCredentialConfigured: item.download_credential_configured,
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

function openDetail(item: Site) {
  selectedSiteId.value = item.id;
  detailDrawer.value = true;
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
  if (!profileConfigurable(selectedProfile.value)) {
    ElMessage.warning('该站点适配器尚未开放，当前版本不能保存或测试');
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
  if (/\r|\n|\0/.test(draft.downloadCookie)) {
    ElMessage.warning('下载 Cookie 不能包含换行或 NUL');
    return false;
  }
  if (draft.type === 'ROUSI_PRO' && requireCredential && !draft.downloadCookie) {
    ElMessage.warning('测试当前表单时必须填写下载 Cookie，不能借用已保存的旧凭证');
    return false;
  }
  if (
    draft.type === 'ROUSI_PRO' &&
    !editing.value &&
    draft.enableAfterSave &&
    !draft.downloadCookie
  ) {
    ElMessage.warning('Rousi Pro 必须分别配置搜索 API Key 和下载 Cookie');
    return false;
  }
  if (requireCredential && !credentialPayload()) {
    ElMessage.warning('测试未保存配置前需要填写站点凭证');
    return false;
  }
  if (!editing.value && draft.enableAfterSave && !credentialPayload()) {
    ElMessage.warning('启用站点必须提供凭证；也可以选择停用后保存。');
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
    ...(draft.type === 'ROUSI_PRO' && draft.downloadCookie
      ? { download_cookie: draft.downloadCookie }
      : {}),
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
    ElMessage.success(
      draft.type === 'ROUSI_PRO'
        ? 'API Key 只读认证通过；下载 Cookie 仍须通过受控取种验收，配置尚未保存'
        : '当前表单只读连接测试通过；配置尚未保存',
    );
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
    current?.type === 'ROUSI_PRO' &&
    draft.credential &&
    current.download_credential_configured &&
    !draft.downloadCookie &&
    !draft.clearDownloadCookie
  ) {
    ElMessage.warning('更换 API Key 会清除旧下载 Cookie，请同时设置新 Cookie 或勾选清除');
    return;
  }
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
      const patch: SitePatchInput = {
        clear_credential: false,
        clear_download_cookie: false,
      };
      if (draft.name.trim() !== current.name) patch.name = draft.name.trim();
      if (draft.type !== current.type) {
        patch.type = draft.type;
      }
      if (draft.clearCredential) {
        patch.clear_credential = true;
      } else if (credential) {
        patch.credential = credential;
      }
      if (draft.type === 'ROUSI_PRO') {
        if (draft.clearDownloadCookie) {
          patch.clear_download_cookie = true;
        } else if (draft.downloadCookie) {
          patch.download_cookie = draft.downloadCookie;
        }
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
      ElMessage.success('站点配置已保存；连接相关变更会自动停用，可重新启用或按需测试连接');
    } else {
      const payload: SiteCreateInput = {
        name: draft.name.trim(),
        type: draft.type,
        ...(credential ? { credential } : {}),
        ...(draft.type === 'ROUSI_PRO' && draft.downloadCookie
          ? { download_cookie: draft.downloadCookie }
          : {}),
        request_timeout_seconds: draft.requestTimeoutSeconds,
        search_interval_seconds: draft.searchIntervalSeconds,
        user_agent: draft.userAgent.trim() || null,
        browser_emulation_enabled: draft.browserEmulationEnabled,
        proxy: proxyCreatePayload(),
      };
      const created = await store.create(payload);
      if (draft.enableAfterSave) {
        try {
          await store.setEnabled(created, true);
          ElMessage.success('站点已保存并启用；可按需执行连接测试');
        } catch (caught) {
          dialog.value = false;
          await refresh(false);
          ElMessage.warning(`站点已保存，但启用失败：${problemText(toApiProblem(caught))}`);
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
    ElMessage.success(
      item.type === 'ROUSI_PRO'
        ? `${item.name} API Key 只读认证通过；下载 Cookie 仍须单独验收`
        : `${item.name} 只读连接测试通过`,
    );
  } catch (caught) {
    await handleWriteProblem(caught);
  }
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
      <div class="site-filters">
        <el-input
          v-model="searchKeyword"
          clearable
          placeholder="搜索站点名称、域名或用户名..."
          class="site-search"
        >
          <template #prefix><Search :size="16" /></template>
        </el-input>
        <el-select v-model="typeFilter" clearable placeholder="站点类型" class="site-filter">
          <el-option
            v-for="profile in profiles"
            :key="profile.kind"
            :label="profile.display_name"
            :value="profile.kind"
          />
        </el-select>
        <el-select v-model="statusFilter" clearable placeholder="连接状态" class="site-filter">
          <el-option label="在线" value="OK" />
          <el-option label="异常" value="FAILED" />
          <el-option label="未测试" value="UNTESTED" />
        </el-select>
        <el-select v-model="sortMode" class="site-filter">
          <el-option label="按名称排序" value="name" />
          <el-option label="按状态排序" value="status" />
          <el-option label="按最近同步排序" value="sync" />
        </el-select>
      </div>
      <div class="site-toolbar-actions">
        <el-button-group>
          <el-button
            :type="viewMode === 'grid' ? 'primary' : 'default'"
            aria-label="卡片视图"
            @click="viewMode = 'grid'"
          >
            <LayoutGrid :size="16" />
          </el-button>
          <el-button
            :type="viewMode === 'list' ? 'primary' : 'default'"
            aria-label="列表视图"
            @click="viewMode = 'list'"
          >
            <List :size="16" />
          </el-button>
        </el-button-group>
        <el-button :loading="loading" @click="refresh()"><RefreshCw :size="15" />刷新</el-button>
        <el-button type="primary" @click="openCreate"><Plus :size="15" />添加站点</el-button>
      </div>
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

    <div v-if="viewMode === 'grid'" v-loading="loading" class="site-cards">
      <article v-for="item in visibleItems" :key="item.id" class="panel site-card">
        <div class="site-card-header">
          <span class="site-card-icon">
            <Globe :size="24" />
            <img
              v-if="faviconUrl(item.base_url)"
              :src="faviconUrl(item.base_url)"
              :alt="item.name"
              loading="lazy"
              @error="hideFavicon"
            />
          </span>
          <div class="site-card-title">
            <div class="site-name-line">
              <h3>{{ item.name }}</h3>
              <span
                class="site-status-dot"
                :class="`site-status-dot--${statusState(item)}`"
                :title="statusText(item)"
              />
            </div>
            <small>{{ kindLabel(item.type) }}</small>
          </div>
          <el-switch
            :model-value="item.enabled"
            :disabled="profileFor(item.type)?.support_status !== 'SUPPORTED'"
            :title="
              profileFor(item.type)?.support_status === 'PENDING_REAL_VALIDATION'
                ? '该站点可配置与测试连接，真实辅种验收完成后才能启用'
                : undefined
            "
            :loading="isBusy(item, 'enable')"
            :aria-label="'启用' + item.name"
            @change="toggleEnabled(item, $event === true)"
          />
        </div>

        <p class="site-endpoint" :title="item.base_url">{{ item.base_url }}</p>

        <div class="site-status-tags">
          <el-tag size="small" :type="statusTag(item.connection_status)">
            {{ connectionLabel(item.connection_status) }}
          </el-tag>
          <el-tag size="small" :type="item.credential_configured ? 'success' : 'info'">
            {{ item.credential_configured ? credentialLabel(item.type) + ' 已配置' : '未配置凭证' }}
          </el-tag>
          <el-tag
            v-if="item.type === 'ROUSI_PRO'"
            size="small"
            :type="item.download_credential_configured ? 'success' : 'info'"
          >
            {{ item.download_credential_configured ? '下载 Cookie 已配置' : '下载 Cookie 未配置' }}
          </el-tag>
        </div>

        <div class="site-summary">
          <div>
            <span>用户名</span>
            <strong>{{ item.user_summary?.username ?? '—' }}</strong>
          </div>
          <div>
            <span>用户等级</span>
            <strong>{{ item.user_summary?.user_level ?? '—' }}</strong>
          </div>
          <div>
            <span>上传量</span>
            <strong>{{
              item.user_summary ? formatBytes(item.user_summary.uploaded_bytes) : '—'
            }}</strong>
          </div>
          <div>
            <span>下载量</span>
            <strong>{{
              item.user_summary ? formatBytes(item.user_summary.downloaded_bytes) : '—'
            }}</strong>
          </div>
          <div>
            <span>分享率</span>
            <strong>{{ item.user_summary ? formatRatio(item.user_summary.ratio) : '—' }}</strong>
          </div>
          <div>
            <span>做种数</span>
            <strong>{{ item.user_summary?.seeding_count ?? '—' }}</strong>
          </div>
          <div class="site-summary-wide">
            <span>最后同步</span>
            <strong>{{
              item.user_summary?.fetched_at
                ? new Date(item.user_summary.fetched_at).toLocaleString()
                : '尚未同步'
            }}</strong>
          </div>
        </div>

        <div class="site-card-actions">
          <el-button type="primary" size="small" @click="openDetail(item)">查看详情</el-button>
          <el-button size="small" :loading="isBusy(item, 'test')" @click="testConnection(item)">
            <Activity :size="14" />测试连接
          </el-button>
          <el-button size="small" @click="openEdit(item)"> <Settings2 :size="14" />编辑 </el-button>
          <el-button link type="danger" :loading="isBusy(item, 'delete')" @click="remove(item)">
            删除
          </el-button>
        </div>
      </article>
    </div>

    <el-table
      v-if="viewMode === 'list' && (visibleItems.length > 0 || loading)"
      v-loading="loading"
      :data="visibleItems"
      class="site-list"
      row-key="id"
      size="default"
    >
      <el-table-column label="站点" min-width="220">
        <template #default="{ row }">
          <div class="site-list-name">
            <span class="site-card-icon site-card-icon--small">
              <Globe :size="19" />
              <img
                v-if="faviconUrl(row.base_url)"
                :src="faviconUrl(row.base_url)"
                :alt="row.name"
                loading="lazy"
                @error="hideFavicon"
              />
            </span>
            <div>
              <strong>{{ row.name }}</strong>
              <small>{{ row.base_url }}</small>
            </div>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="110">
        <template #default="{ row }">{{ typeLabel(row.type) }}</template>
      </el-table-column>
      <el-table-column label="连接状态" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="statusTag(row.connection_status)">{{
            statusLabel(row.connection_status)
          }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="用户名" width="130" show-overflow-tooltip>
        <template #default="{ row }">{{ row.user_summary?.username ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="分享率" width="90">
        <template #default="{ row }">{{ formatRatio(row.user_summary?.ratio) }}</template>
      </el-table-column>
      <el-table-column label="最后同步" width="170">
        <template #default="{ row }">{{
          row.user_summary?.fetched_at
            ? new Date(row.user_summary.fetched_at).toLocaleString()
            : '尚未同步'
        }}</template>
      </el-table-column>
      <el-table-column label="操作" width="210" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openDetail(row)">查看详情</el-button>
          <el-button link @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" :loading="isBusy(row, 'delete')" @click="remove(row)"
            >删除</el-button
          >
        </template>
      </el-table-column>
    </el-table>

    <el-empty
      v-if="!loading && items.length > 0 && !visibleItems.length && !error"
      description="没有匹配的站点"
    />

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
                :label="siteProfileOptionLabel(profile)"
                :value="profile.kind"
                :disabled="!profileConfigurable(profile)"
              />
            </el-select>
          </el-form-item>
        </div>

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

        <template v-if="draft.type === 'ROUSI_PRO'">
          <el-alert
            title="搜索使用 API Key，获取 Torrent 使用独立 Cookie；两项凭证分开加密存储。"
            type="info"
            :closable="false"
            class="form-alert"
          />
          <el-form-item label="下载 Cookie">
            <el-input
              v-model="draft.downloadCookie"
              type="password"
              show-password
              autocomplete="new-password"
              :disabled="draft.clearDownloadCookie || !profileConfigurable(selectedProfile)"
              :placeholder="
                editing && draft.downloadCredentialConfigured
                  ? '留空保留原下载 Cookie'
                  : '请输入独立下载 Cookie'
              "
            />
          </el-form-item>
          <el-checkbox
            v-if="editing && draft.downloadCredentialConfigured"
            v-model="draft.clearDownloadCookie"
            >清除现有下载 Cookie 并保持站点停用</el-checkbox
          >
        </template>

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

        <el-form-item v-if="!editing" label="是否启用">
          <el-radio-group v-model="draft.enableAfterSave">
            <el-radio :value="true">启用</el-radio>
            <el-radio :value="false">停用</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button
          v-if="!editing || draft.credential"
          :loading="probingDraft"
          :disabled="!profileConfigurable(selectedProfile)"
          @click="testDraftConnection"
          >测试当前表单</el-button
        >
        <el-button
          type="primary"
          :loading="saving"
          :disabled="!profileConfigurable(selectedProfile)"
          @click="save"
          >保存配置</el-button
        >
      </template>
    </el-dialog>

    <SiteDetailDrawer v-model="detailDrawer" :site-id="selectedSiteId" />
  </div>
</template>

<style scoped>
.site-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.site-filters {
  display: flex;
  min-width: 0;
  flex: 1;
  gap: 8px;
}

.site-search {
  width: min(290px, 30vw);
}

.site-filter {
  width: 135px;
}

.site-toolbar-actions {
  display: flex;
  align-items: center;
  flex-shrink: 0;
  gap: 8px;
}

.site-list {
  width: 100%;
}

.site-list-name {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 10px;
}

.site-list-name > div {
  min-width: 0;
}

.site-list-name strong,
.site-list-name small {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.site-list-name small {
  margin-top: 2px;
  color: var(--muted);
  font-size: 11px;
}

.site-card-icon--small {
  flex-basis: 38px;
  width: 38px;
  height: 38px;
}

@media (max-width: 1100px) {
  .site-toolbar {
    align-items: stretch;
    flex-direction: column;
  }

  .site-filters {
    flex-wrap: wrap;
  }

  .site-search {
    width: min(100%, 320px);
  }
}

@media (max-width: 700px) {
  .site-filter {
    flex: 1 1 140px;
  }

  .site-toolbar-actions {
    justify-content: flex-end;
  }
}

.site-cards {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.site-card {
  display: flex;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  overflow: hidden;
}

.site-card-header {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 10px;
}

.site-card-icon {
  position: relative;
  display: grid;
  place-items: center;
  flex: 0 0 44px;
  width: 44px;
  height: 44px;
  border-radius: 10px;
  background: var(--surface-soft);
  color: var(--blue);
}

.site-card-icon img {
  position: absolute;
  inset: 4px;
  width: calc(100% - 8px);
  height: calc(100% - 8px);
  border-radius: 7px;
  object-fit: contain;
  background: var(--surface);
}

.site-card-title {
  min-width: 0;
  flex: 1;
}

.site-card-title small {
  color: var(--muted);
  font-size: 11px;
}

.site-name-line {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 7px;
}

.site-name-line h3 {
  min-width: 0;
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.site-endpoint {
  margin: 0;
  overflow: hidden;
  color: var(--muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.site-status-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.site-summary {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 7px 12px;
  padding-top: 2px;
}

.site-summary > div {
  display: flex;
  min-width: 0;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
}

.site-summary span {
  color: var(--muted);
  white-space: nowrap;
}

.site-summary strong {
  min-width: 0;
  overflow: hidden;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.site-summary-wide {
  grid-column: 1 / -1;
}

.site-card-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: auto;
  padding-top: 9px;
  border-top: 1px solid var(--line);
}

.site-card-actions .el-button {
  margin-left: 0;
}

@media (max-width: 1180px) {
  .site-cards {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 1050px) {
  .site-cards {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 800px) {
  .site-cards {
    grid-template-columns: 1fr;
  }
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
