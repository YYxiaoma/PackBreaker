<script setup lang="ts">
import { ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { Activity, BarChart3, Clock3, Globe, Link2, RefreshCw, UserRound } from '@lucide/vue';

import { toApiProblem } from '../api/client';
import {
  getSiteDetail,
  listSiteSeeding,
  listSiteSyncHistory,
  syncSiteDetail,
  type SiteDetail,
  type SiteSeeding,
  type SiteSyncHistoryItem,
} from '../api/sites';

const props = defineProps<{
  modelValue: boolean;
  siteId: string | null;
}>();

const emit = defineEmits<{
  'update:modelValue': [value: boolean];
}>();

const activeTab = ref('overview');
const detail = ref<SiteDetail | null>(null);
const detailCache = new Map<string, SiteDetail>();
let detailRequestSequence = 0;
let historyRequestSequence = 0;
let seedingRequestSequence = 0;
const history = ref<SiteSyncHistoryItem[]>([]);
const seeding = ref<SiteSeeding | null>(null);
const loading = ref(false);
const syncing = ref(false);
const historyLoading = ref(false);
const seedingLoading = ref(false);
const error = ref('');

watch(
  () => [props.modelValue, props.siteId] as const,
  ([open, siteId], [wasOpen, previousSiteId]) => {
    if (!open) {
      // Invalidate in-flight requests when the drawer closes so late responses cannot
      // update state after the user has left the detail view.
      detailRequestSequence += 1;
      historyRequestSequence += 1;
      seedingRequestSequence += 1;
      loading.value = false;
      historyLoading.value = false;
      seedingLoading.value = false;
      return;
    }

    if (siteId !== previousSiteId || !wasOpen) {
      historyRequestSequence += 1;
      seedingRequestSequence += 1;
      history.value = [];
      seeding.value = null;
      historyLoading.value = false;
      seedingLoading.value = false;
    }
    void loadDetail();
    if (activeTab.value === 'history') void loadHistory();
    if (activeTab.value === 'seeding') void loadSeeding();
  },
);

watch(activeTab, (tab) => {
  if (!props.modelValue || !props.siteId) return;
  if (tab === 'history') void loadHistory();
  if (tab === 'seeding') void loadSeeding();
});

async function loadDetail() {
  const siteId = props.siteId;
  if (!siteId) return;

  const requestSequence = ++detailRequestSequence;
  const cached = detailCache.get(siteId);
  if (cached) detail.value = cached;
  else if (detail.value?.site.id !== siteId) detail.value = null;

  loading.value = true;
  error.value = '';
  try {
    const loaded = await getSiteDetail(siteId);
    if (requestSequence !== detailRequestSequence || props.siteId !== siteId || !props.modelValue) {
      return;
    }
    detailCache.set(siteId, loaded);
    detail.value = loaded;
    if (loaded.connection.last_sync_at) {
      const age = Date.now() - new Date(loaded.connection.last_sync_at).getTime();
      if (age > 10 * 60 * 1000) void syncNow(true, siteId);
    }
  } catch (caught) {
    if (requestSequence !== detailRequestSequence || props.siteId !== siteId) return;
    const latestSuccessful = detailCache.get(siteId);
    if (latestSuccessful) detail.value = latestSuccessful;
    else if (detail.value?.site.id !== siteId) detail.value = null;
    error.value = toApiProblem(caught).message;
  } finally {
    if (requestSequence === detailRequestSequence) loading.value = false;
  }
}

async function syncNow(silent = false, siteId = props.siteId) {
  if (!siteId || syncing.value) return;
  syncing.value = true;
  error.value = '';
  try {
    const synced = await syncSiteDetail(siteId);
    detailCache.set(siteId, synced);
    if (props.siteId === siteId) {
      detail.value = synced;
      await loadHistory();
      if (!silent) ElMessage.success('站点用户信息同步完成');
    }
  } catch (caught) {
    if (props.siteId !== siteId) return;
    const problem = toApiProblem(caught);
    error.value = problem.traceId
      ? problem.message + ' · trace_id ' + problem.traceId
      : problem.message;
    if (!silent) ElMessage.error(error.value);
  } finally {
    syncing.value = false;
  }
}
async function loadHistory() {
  const siteId = props.siteId;
  if (!siteId || !props.modelValue) return;
  const requestSequence = ++historyRequestSequence;
  historyLoading.value = true;
  try {
    const loaded = await listSiteSyncHistory(siteId);
    if (
      requestSequence !== historyRequestSequence ||
      props.siteId !== siteId ||
      !props.modelValue
    ) {
      return;
    }
    history.value = loaded;
  } catch (caught) {
    if (requestSequence === historyRequestSequence && props.siteId === siteId && props.modelValue) {
      ElMessage.error(toApiProblem(caught).message);
    }
  } finally {
    if (requestSequence === historyRequestSequence) historyLoading.value = false;
  }
}

async function loadSeeding() {
  const siteId = props.siteId;
  if (!siteId || !props.modelValue) return;
  const requestSequence = ++seedingRequestSequence;
  seedingLoading.value = true;
  try {
    const loaded = await listSiteSeeding(siteId);
    if (
      requestSequence !== seedingRequestSequence ||
      props.siteId !== siteId ||
      !props.modelValue
    ) {
      return;
    }
    seeding.value = loaded;
  } catch (caught) {
    if (requestSequence === seedingRequestSequence && props.siteId === siteId && props.modelValue) {
      ElMessage.error(toApiProblem(caught).message);
    }
  } finally {
    if (requestSequence === seedingRequestSequence) seedingLoading.value = false;
  }
}

function close() {
  emit('update:modelValue', false);
}

function valueOrDash(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  return String(value);
}

function formatDate(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : '—';
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

function connectionTagType(status: SiteDetail['site']['connection_status']) {
  if (status === 'OK') return 'success';
  if (status === 'FAILED') return 'danger';
  return 'info';
}

function connectionLabel(status: SiteDetail['site']['connection_status']) {
  if (status === 'OK') return '在线';
  if (status === 'FAILED') return '异常';
  return '未测试';
}

function syncStatusType(status: SiteSyncHistoryItem['status']) {
  return status === 'SUCCESS' ? 'success' : 'danger';
}

function hnrTagType(status: string | null | undefined) {
  return status === '正常' || status === 'Normal' ? 'success' : status ? 'danger' : 'info';
}

const FEATURE_CAPABILITY_LABELS: Record<keyof SiteDetail['capabilities'], string> = {
  search: '搜索',
  torrent_detail: '种子详情',
  torrent_download: '种子下载',
  user_info: '用户信息',
  seeding_info: '做种信息',
  api: 'API',
  cookie: 'Cookie',
  browser_emulation: '浏览器仿真',
  proxy: '代理',
};

function featureCapabilityLabels(capabilities: SiteDetail['capabilities']): string[] {
  return (Object.keys(FEATURE_CAPABILITY_LABELS) as Array<keyof SiteDetail['capabilities']>)
    .filter((key) => capabilities[key])
    .map((key) => FEATURE_CAPABILITY_LABELS[key]);
}

function supportStatusLabel(status: SiteDetail['support_status']): string {
  if (status === 'SUPPORTED') return '已支持';
  if (status === 'PENDING_REAL_VALIDATION') return '待真实验收';
  return '待适配';
}

function credentialKindLabel(kind: string): string {
  return kind === 'API_KEY' ? 'API Key' : kind === 'COOKIE' ? 'Cookie' : kind;
}

function siteTypeLabel(type: string): string {
  return type === 'MTEAM' ? '综合类' : type === 'HDTIME' || type === 'HDFANS' ? '高清影视' : type;
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
</script>

<template>
  <el-drawer
    :model-value="modelValue"
    direction="rtl"
    size="min(500px, 42vw)"
    :with-header="false"
    class="site-detail-drawer"
    @update:model-value="emit('update:modelValue', $event)"
    @closed="close"
  >
    <div class="site-detail">
      <header class="site-detail-header">
        <div class="site-detail-heading">
          <span class="site-detail-logo">
            <Globe :size="25" />
            <img
              v-if="detail && faviconUrl(detail.site.base_url)"
              :src="detail ? faviconUrl(detail.site.base_url) : ''"
              :alt="detail?.site.name ?? ''"
              @error="hideFavicon"
            />
          </span>
          <div class="site-detail-title">
            <div class="site-detail-name-row">
              <h2>{{ detail?.site.name ?? '站点详情' }}</h2>
              <el-tag v-if="detail" size="small">{{ siteTypeLabel(detail.site.type) }}</el-tag>
            </div>
            <div v-if="detail" class="site-detail-status-row">
              <el-tag size="small" :type="connectionTagType(detail.site.connection_status)">
                ● {{ connectionLabel(detail.site.connection_status) }}
              </el-tag>
              <span>{{ detail.site.base_url }}</span>
            </div>
          </div>
        </div>
        <div class="site-detail-header-actions">
          <el-button type="primary" :loading="syncing" :disabled="!detail" @click="syncNow">
            <RefreshCw :size="15" />立即同步
          </el-button>
          <el-button text aria-label="关闭详情" @click="close">×</el-button>
        </div>
      </header>

      <div v-loading="loading" class="site-detail-body">
        <el-alert v-if="error" :title="error" type="warning" :closable="false" show-icon />

        <el-tabs v-model="activeTab" class="site-detail-tabs">
          <el-tab-pane label="概览" name="overview">
            <template v-if="detail">
              <section class="detail-section">
                <div class="detail-section-title"><UserRound :size="17" />账户信息</div>
                <dl class="detail-list">
                  <div>
                    <dt>用户名</dt>
                    <dd>{{ valueOrDash(detail.account.username) }}</dd>
                  </div>
                  <div>
                    <dt>用户 ID</dt>
                    <dd>{{ valueOrDash(detail.account.uid) }}</dd>
                  </div>
                  <div>
                    <dt>用户等级</dt>
                    <dd>{{ valueOrDash(detail.account.user_level) }}</dd>
                  </div>
                  <div>
                    <dt>加入时间</dt>
                    <dd>{{ formatDate(detail.account.join_time) }}</dd>
                  </div>
                  <div>
                    <dt>最后访问</dt>
                    <dd>{{ formatDate(detail.account.last_access_at) }}</dd>
                  </div>
                  <div>
                    <dt>H&R 状态</dt>
                    <dd>
                      <el-tag
                        v-if="detail.account.hnr_status"
                        size="small"
                        :type="hnrTagType(detail.account.hnr_status)"
                      >
                        {{ detail.account.hnr_status }}
                      </el-tag>
                      <span v-else>—</span>
                    </dd>
                  </div>
                  <div>
                    <dt>H&R 次数</dt>
                    <dd>{{ valueOrDash(detail.account.hnr_count) }}</dd>
                  </div>
                  <div>
                    <dt>消息数</dt>
                    <dd>{{ valueOrDash(detail.account.message_count) }}</dd>
                  </div>
                  <div>
                    <dt>备注</dt>
                    <dd>{{ valueOrDash(detail.account.remark) }}</dd>
                  </div>
                </dl>
              </section>

              <section class="detail-section">
                <div class="detail-section-title"><BarChart3 :size="17" />数据统计</div>
                <div class="stat-grid">
                  <div>
                    <span>上传量</span
                    ><strong>{{ formatBytes(detail.statistics.uploaded_bytes) }}</strong>
                  </div>
                  <div>
                    <span>下载量</span
                    ><strong>{{ formatBytes(detail.statistics.downloaded_bytes) }}</strong>
                  </div>
                  <div>
                    <span>分享率</span><strong>{{ formatRatio(detail.statistics.ratio) }}</strong>
                  </div>
                  <div>
                    <span>做种数</span
                    ><strong>{{ valueOrDash(detail.statistics.seeding_count) }}</strong>
                  </div>
                  <div>
                    <span>做种量</span
                    ><strong>{{ formatBytes(detail.statistics.seeding_size_bytes) }}</strong>
                  </div>
                  <div>
                    <span>发种数</span
                    ><strong>{{ valueOrDash(detail.statistics.torrents_posted) }}</strong>
                  </div>
                  <div>
                    <span>魔力值/积分</span
                    ><strong>{{ valueOrDash(detail.statistics.bonus) }}</strong>
                  </div>
                  <div>
                    <span>做种积分</span
                    ><strong>{{ valueOrDash(detail.statistics.seeding_points) }}</strong>
                  </div>
                  <div>
                    <span>每小时魔力值/积分</span
                    ><strong>{{ valueOrDash(detail.statistics.bonus_per_hour) }}</strong>
                  </div>
                </div>
              </section>

              <section class="detail-section">
                <div class="detail-section-title"><Link2 :size="17" />站点信息</div>
                <dl class="detail-list">
                  <div>
                    <dt>站点类型</dt>
                    <dd>{{ siteTypeLabel(detail.site.type) }}</dd>
                  </div>
                  <div>
                    <dt>站点状态</dt>
                    <dd>{{ detail.site.enabled ? '启用' : '停用' }}</dd>
                  </div>
                  <div>
                    <dt>站点地址</dt>
                    <dd class="breakable">{{ detail.site.base_url }}</dd>
                  </div>
                  <div>
                    <dt>凭证类型</dt>
                    <dd>{{ credentialKindLabel(detail.site.credential_kind) }}</dd>
                  </div>
                  <div>
                    <dt>凭证状态</dt>
                    <dd>{{ detail.connection.credential_configured ? '已配置' : '未配置' }}</dd>
                  </div>
                  <div>
                    <dt>支持功能</dt>
                    <dd>{{ featureCapabilityLabels(detail.capabilities).join('、') || '—' }}</dd>
                  </div>
                </dl>
              </section>

              <section class="detail-section">
                <div class="detail-section-title"><Activity :size="17" />连接状态</div>
                <dl class="detail-list">
                  <div>
                    <dt>连接状态</dt>
                    <dd>
                      <el-tag
                        size="small"
                        :type="connectionTagType(detail.site.connection_status)"
                        >{{ connectionLabel(detail.site.connection_status) }}</el-tag
                      >
                    </dd>
                  </div>
                  <div>
                    <dt>最近测试</dt>
                    <dd>{{ formatDate(detail.connection.last_test_at) }}</dd>
                  </div>
                  <div>
                    <dt>最近同步</dt>
                    <dd>{{ formatDate(detail.connection.last_sync_at) }}</dd>
                  </div>
                  <div>
                    <dt>最近同步耗时</dt>
                    <dd>
                      {{
                        detail.connection.last_sync_duration_ms !== null
                          ? detail.connection.last_sync_duration_ms + ' ms'
                          : '—'
                      }}
                    </dd>
                  </div>
                  <div>
                    <dt>可靠性状态</dt>
                    <dd>{{ detail.connection.circuit_state }}</dd>
                  </div>
                  <div>
                    <dt>最近错误码</dt>
                    <dd>{{ valueOrDash(detail.connection.last_error_code) }}</dd>
                  </div>
                </dl>
              </section>
            </template>
            <el-empty v-else-if="!loading" description="暂无同步数据" />
          </el-tab-pane>

          <el-tab-pane label="详细信息" name="details">
            <template v-if="detail">
              <section class="detail-section">
                <div class="detail-section-title"><Globe :size="17" />站点基础</div>
                <dl class="detail-list">
                  <div>
                    <dt>站点名称</dt>
                    <dd>{{ detail.site.name }}</dd>
                  </div>
                  <div>
                    <dt>SiteKind</dt>
                    <dd>{{ detail.site.type }}（{{ siteTypeLabel(detail.site.type) }}）</dd>
                  </div>
                  <div>
                    <dt>Base URL</dt>
                    <dd class="breakable">{{ detail.site.base_url }}</dd>
                  </div>
                  <div>
                    <dt>凭证类型</dt>
                    <dd>{{ credentialKindLabel(detail.site.credential_kind) }}</dd>
                  </div>
                  <div>
                    <dt>支持状态</dt>
                    <dd>
                      <el-tag
                        size="small"
                        :type="detail.support_status === 'SUPPORTED' ? 'success' : 'warning'"
                      >
                        {{ supportStatusLabel(detail.support_status) }}
                      </el-tag>
                    </dd>
                  </div>
                  <div>
                    <dt>站点版本</dt>
                    <dd>{{ valueOrDash(detail.site_version) }}</dd>
                  </div>
                  <div>
                    <dt>配置版本</dt>
                    <dd>v{{ detail.config_version }}</dd>
                  </div>
                </dl>
              </section>
              <section class="detail-section">
                <div class="detail-section-title"><Clock3 :size="17" />访问配置</div>
                <dl class="detail-list">
                  <div>
                    <dt>请求超时</dt>
                    <dd>{{ detail.site.request_timeout_seconds }} 秒</dd>
                  </div>
                  <div>
                    <dt>搜索间隔</dt>
                    <dd>{{ detail.site.search_interval_seconds }} 秒</dd>
                  </div>
                  <div>
                    <dt>User-Agent</dt>
                    <dd>{{ detail.site.user_agent ? '已配置' : '未配置' }}</dd>
                  </div>
                  <div>
                    <dt>浏览器仿真</dt>
                    <dd>{{ detail.site.browser_emulation_enabled ? '已启用' : '未启用' }}</dd>
                  </div>
                  <div>
                    <dt>独立代理</dt>
                    <dd>
                      {{
                        detail.site.proxy_enabled
                          ? (detail.site.proxy_host ?? '—') + ':' + (detail.site.proxy_port ?? '—')
                          : '未启用'
                      }}
                    </dd>
                  </div>
                </dl>
              </section>
              <section class="detail-section">
                <div class="detail-section-title"><Link2 :size="17" />能力</div>
                <div class="capability-tags">
                  <el-tag
                    v-for="label in featureCapabilityLabels(detail.capabilities)"
                    :key="label"
                    size="small"
                    type="info"
                  >
                    {{ label }}
                  </el-tag>
                  <span
                    v-if="!featureCapabilityLabels(detail.capabilities).length"
                    class="empty-capabilities"
                  >
                    暂无已确认能力
                  </span>
                </div>
              </section>
            </template>
          </el-tab-pane>

          <el-tab-pane label="做种列表" name="seeding">
            <div v-loading="seedingLoading">
              <el-empty
                v-if="seeding && !seeding.supported"
                description="当前站点适配器未提供做种列表能力"
              />
              <el-table v-else-if="seeding?.items.length" :data="seeding.items" size="small">
                <el-table-column
                  prop="title"
                  label="种子名称"
                  min-width="180"
                  show-overflow-tooltip
                />
                <el-table-column label="大小" width="95">
                  <template #default="{ row }">{{ formatBytes(row.size_bytes) }}</template>
                </el-table-column>
                <el-table-column prop="status" label="状态" width="80" />
              </el-table>
              <el-empty v-else-if="!seedingLoading" description="暂无做种数据" />
            </div>
          </el-tab-pane>

          <el-tab-pane label="历史同步" name="history">
            <div v-loading="historyLoading">
              <el-table v-if="history.length" :data="history" size="small">
                <el-table-column label="同步时间" min-width="150">
                  <template #default="{ row }">{{ formatDate(row.started_at) }}</template>
                </el-table-column>
                <el-table-column label="状态" width="70">
                  <template #default="{ row }">
                    <el-tag size="small" :type="syncStatusType(row.status)">
                      {{ row.status === 'SUCCESS' ? '成功' : '失败' }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="耗时" width="70">
                  <template #default="{ row }">{{ row.duration_ms }} ms</template>
                </el-table-column>
                <el-table-column label="字段" width="55" prop="field_count" />
              </el-table>
              <el-empty v-else-if="!historyLoading" description="暂无同步记录" />
            </div>
          </el-tab-pane>
        </el-tabs>
      </div>
    </div>
  </el-drawer>
</template>

<style scoped>
.site-detail {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
}

.site-detail-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  padding: 18px 20px 14px;
  border-bottom: 1px solid var(--line);
}

.site-detail-heading {
  display: flex;
  min-width: 0;
  gap: 12px;
}

.site-detail-logo {
  position: relative;
  display: grid;
  place-items: center;
  flex: 0 0 52px;
  width: 52px;
  height: 52px;
  border-radius: 10px;
  background: var(--ink);
  color: #fff;
}

.site-detail-logo img {
  position: absolute;
  inset: 4px;
  width: calc(100% - 8px);
  height: calc(100% - 8px);
  border-radius: 8px;
  object-fit: contain;
  background: var(--surface);
}

.site-detail-title {
  min-width: 0;
}

.site-detail-name-row,
.site-detail-status-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.site-detail-name-row h2 {
  margin: 0;
  font-size: 20px;
}

.site-detail-status-row {
  margin-top: 7px;
  color: var(--muted);
  font-size: 12px;
}

.site-detail-status-row span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.site-detail-header-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 0 0 auto;
}

.site-detail-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 14px 18px 22px;
}

.site-detail-tabs :deep(.el-tabs__header) {
  margin-bottom: 12px;
}

.detail-section {
  padding: 14px 0;
  border-bottom: 1px solid var(--line);
}

.detail-section:last-child {
  border-bottom: 0;
}

.detail-section-title {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 12px;
  font-weight: 650;
}

.detail-list {
  display: grid;
  gap: 9px;
  margin: 0;
}

.detail-list > div {
  display: grid;
  grid-template-columns: 110px minmax(0, 1fr);
  gap: 10px;
  align-items: center;
  font-size: 13px;
}

.detail-list dt {
  color: var(--muted);
}

.detail-list dd {
  margin: 0;
  text-align: right;
  overflow-wrap: anywhere;
}

.detail-list dd.breakable {
  text-align: left;
}

.capability-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}

.empty-capabilities {
  color: var(--muted);
  font-size: 12px;
}

.stat-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 9px;
}

.stat-grid > div {
  padding: 10px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface-soft);
}

.stat-grid span,
.stat-grid strong {
  display: block;
}

.stat-grid span {
  color: var(--muted);
  font-size: 11px;
}

.stat-grid strong {
  margin-top: 4px;
  font-size: 14px;
}

@media (max-width: 800px) {
  .site-detail-header {
    padding-inline: 14px;
  }

  .site-detail-body {
    padding-inline: 14px;
  }

  .site-detail-header-actions .el-button:not(.el-button--primary) {
    padding-inline: 5px;
  }

  .stat-grid {
    grid-template-columns: 1fr;
  }

  :global(.site-detail-drawer) {
    width: 100% !important;
  }
}
</style>
