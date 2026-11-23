<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { Activity, RefreshCw, Search, Settings2 } from '@lucide/vue';
import { ElMessage } from 'element-plus';

import SeedingHealthDetailDrawer from './SeedingHealthDetailDrawer.vue';
import { listDownloaders, type Downloader } from '../api/downloaders';
import {
  getSeedingHealthSettings,
  getSeedingHealthSummary,
  listSeedingIssues,
  listSeedingPolicies,
  setSeedingHealthSettings,
  setSeedingPolicy,
  triggerSeedingHealthRun,
  type SeedingHealthSettings,
  type SeedingHealthSummary,
  type SeedingIssueRow,
  type SeedingPolicy,
} from '../api/monitoring';
import { listSites, type Site } from '../api/sites';
import { toApiProblem } from '../api/client';
import {
  evidenceSummary,
  formatBytes,
  kindLabel,
  severityLabel,
  severityTone,
  suggestionLabel,
} from '../monitoringPresentation';

const emit = defineEmits<{ navigate: [route: string] }>();

const summary = ref<SeedingHealthSummary | null>(null);
const items = ref<SeedingIssueRow[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(20);
const loading = ref(false);
const running = ref(false);
const errorText = ref('');
const activeTab = ref('ALL');
const showResolved = ref(false);
const search = ref('');
const siteFilter = ref('');
const downloaderFilter = ref('');
const severityFilter = ref('');
const detailId = ref<string | null>(null);
let issueSequence = 0;
let pollCancelled = false;
const sites = ref<Site[]>([]);
const downloaders = ref<Downloader[]>([]);
const settingsDialogVisible = ref(false);
const settings = ref<SeedingHealthSettings>({ site_probe_enabled: false });
const settingsSaving = ref(false);
const policies = ref<SeedingPolicy[]>([]);
const policyRows = ref<{ siteKey: string; name: string; value: number | null; saving: boolean }[]>(
  [],
);

const tabs = computed(() => {
  const byKind = summary.value?.by_kind ?? {};
  const totalAbnormal = summary.value?.abnormal ?? 0;
  return [
    { key: 'ALL', label: '全部', count: totalAbnormal },
    { key: 'SITE_MISSING', label: '站侧无记录', count: byKind.SITE_MISSING ?? 0 },
    { key: 'SITE_ONLY', label: '站侧独有', count: byKind.SITE_ONLY ?? 0 },
    { key: 'TRACKER_ERROR', label: 'Tracker 报错', count: byKind.TRACKER_ERROR ?? 0 },
    { key: 'FILE_MISSING', label: '文件缺失', count: byKind.FILE_MISSING ?? 0 },
  ];
});

const siteNameByKey = computed(() => {
  const map = new Map<string, string>();
  for (const site of sites.value) map.set(site.type.toLowerCase(), site.name);
  return map;
});

const downloaderNameById = computed(() => {
  const map = new Map<string, string>();
  for (const item of downloaders.value) map.set(item.id, item.name);
  return map;
});

const hasNeverRun = computed(() => summary.value !== null && summary.value.last_run === null);
const isEmptyAfterRun = computed(
  () => !loading.value && items.value.length === 0 && (summary.value?.last_run ?? null) !== null,
);

function siteLabel(key: string | null): string {
  if (!key) return '未关联';
  return siteNameByKey.value.get(key) ?? key.toUpperCase();
}

function downloaderLabel(id: string | null | undefined): string {
  if (!id) return '—';
  return downloaderNameById.value.get(id) ?? id.slice(0, 8);
}

async function loadSummary(): Promise<void> {
  try {
    summary.value = await getSeedingHealthSummary();
  } catch (error) {
    errorText.value = toApiProblem(error).message;
  }
}

async function loadIssues(): Promise<void> {
  const sequence = ++issueSequence;
  loading.value = true;
  try {
    const data = await listSeedingIssues({
      kind: activeTab.value === 'ALL' ? undefined : activeTab.value,
      status: showResolved.value ? 'ALL' : undefined,
      severity: severityFilter.value || undefined,
      siteKey: siteFilter.value || undefined,
      downloaderId: downloaderFilter.value || undefined,
      search: search.value.trim() || undefined,
      page: page.value,
      pageSize: pageSize.value,
    });
    if (sequence !== issueSequence) return;
    items.value = data.items;
    total.value = data.total;
    errorText.value = '';
  } catch (error) {
    if (sequence === issueSequence) errorText.value = toApiProblem(error).message;
  } finally {
    if (sequence === issueSequence) loading.value = false;
  }
}

async function loadSettings(): Promise<void> {
  try {
    settings.value = await getSeedingHealthSettings();
  } catch (error) {
    ElMessage.error(toApiProblem(error).message);
  }
}

async function loadPolicies(): Promise<void> {
  try {
    policies.value = await listSeedingPolicies();
  } catch (error) {
    ElMessage.error(toApiProblem(error).message);
    return;
  }
  policyRows.value = sites.value.map((site) => {
    const siteKey = site.type.toLowerCase();
    return {
      siteKey,
      name: site.name,
      value: policies.value.find((item) => item.site_key === siteKey)?.min_seeding_hours ?? null,
      saving: false,
    };
  });
}

async function saveSiteProbeEnabled(enabled: boolean): Promise<void> {
  settingsSaving.value = true;
  try {
    settings.value = await setSeedingHealthSettings(enabled);
    ElMessage.success(enabled ? '站点侧核对已开启，下一轮巡查生效' : '站点侧核对已关闭');
  } catch (error) {
    settings.value.site_probe_enabled = !enabled;
    ElMessage.error(toApiProblem(error).message);
  } finally {
    settingsSaving.value = false;
  }
}

async function savePolicy(row: {
  siteKey: string;
  value: number | null;
  saving: boolean;
}): Promise<void> {
  const previous = row.value;
  row.saving = true;
  try {
    const saved = await setSeedingPolicy(row.siteKey, row.value);
    row.value = saved.min_seeding_hours;
    const index = policies.value.findIndex((item) => item.site_key === row.siteKey);
    if (index >= 0) policies.value[index] = saved;
    else policies.value.push(saved);
    ElMessage.success('H&R 规则已保存，下一轮巡查会重算风险分');
    await loadSummary();
  } catch (error) {
    row.value = previous;
    ElMessage.error(toApiProblem(error).message);
  } finally {
    row.saving = false;
  }
}

function openSettings(): void {
  settingsDialogVisible.value = true;
  void loadSettings();
  void loadPolicies();
}

async function refresh(): Promise<void> {
  await Promise.all([loadSummary(), loadIssues()]);
}

function selectTab(key: string): void {
  activeTab.value = key;
  page.value = 1;
  void loadIssues();
}

function applyFilters(): void {
  page.value = 1;
  void loadIssues();
}

function resetFilters(): void {
  search.value = '';
  siteFilter.value = '';
  downloaderFilter.value = '';
  severityFilter.value = '';
  showResolved.value = false;
  page.value = 1;
  void loadIssues();
}

async function runNow(): Promise<void> {
  if (running.value) return;
  running.value = true;
  try {
    const result = await triggerSeedingHealthRun();
    if (!result.accepted) {
      ElMessage.warning('已有巡查正在进行，请稍候');
      return;
    }
    ElMessage.success('巡查已开始，完成后会自动刷新');
    await pollUntilFinished();
    await refresh();
  } catch (error) {
    ElMessage.error(toApiProblem(error).message);
  } finally {
    running.value = false;
  }
}

async function pollUntilFinished(): Promise<void> {
  pollCancelled = false;
  for (let attempt = 0; attempt < 40 && !pollCancelled; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    if (pollCancelled) return;
    await loadSummary();
    const status = summary.value?.last_run?.status;
    if (status && status !== 'RUNNING') return;
  }
}

function openDetail(row: SeedingIssueRow): void {
  detailId.value = row.id;
}

function onDetailChanged(): void {
  void refresh();
}

function formatTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('zh-CN', { hour12: false });
}

let timer: number | undefined;

onMounted(async () => {
  await refresh();
  try {
    const [siteList, downloaderList] = await Promise.all([listSites(), listDownloaders()]);
    sites.value = siteList;
    downloaders.value = downloaderList;
    void loadPolicies();
  } catch {
    // 名称映射失败不阻断列表展示，回退显示原始标识。
  }
  timer = window.setInterval(() => {
    if (!running.value) void loadSummary();
  }, 30_000);
});

onUnmounted(() => {
  pollCancelled = true;
  if (timer) window.clearInterval(timer);
});
</script>

<template>
  <section class="seeding-health">
    <header class="page-head">
      <div>
        <h1>做种健康巡查</h1>
        <p class="subtitle">
          定期比对下载器与站点侧做种记录，提前发现删种、Tracker 异常与文件缺失；站点侧核对默认关闭，
          开启后按站点预算抽样轮转
        </p>
      </div>
      <div class="head-actions">
        <div class="run-meta">
          <span>最近巡查</span>
          <b>{{ formatTime(summary?.last_run?.finished_at) }}</b>
          <span>下次巡查</span>
          <b>{{ formatTime(summary?.next_run_at) }}</b>
        </div>
        <button class="ghost-button" type="button" @click="openSettings">
          <Settings2 :size="16" />
          巡查设置
        </button>
        <button class="primary-button" type="button" :disabled="running" @click="runNow">
          <RefreshCw :size="16" :class="{ spinning: running }" />
          {{ running ? '巡查中…' : '立即巡查' }}
        </button>
      </div>
    </header>

    <p v-if="summary?.stale" class="banner warning">
      巡查数据可能已过期，请确认后台调度是否正常运行。
    </p>
    <p v-if="summary?.last_run?.degraded_sites.length" class="banner warning">
      本次有站点未完成判定（{{ summary.last_run.degraded_sites.join('、') }}），已跳过以避免误报，
      请检查这些站点的凭证。
    </p>
    <p v-if="summary?.coverage.unmapped" class="banner warning">
      有 {{ summary.coverage.unmapped }} 个种子的路径未被下载器路径映射覆盖，本轮没有做文件检查。
      请到「配置中心 / 下载器配置」补齐映射规则后再看结果。
    </p>
    <p v-if="errorText" class="banner danger">{{ errorText }}</p>

    <div class="stat-grid">
      <article class="stat-card">
        <span>巡查种子</span>
        <strong>{{ summary?.watched ?? 0 }}</strong>
        <small>下载器内已完成下载的任务</small>
      </article>
      <article class="stat-card">
        <span>正常做种</span>
        <strong class="ok">{{ summary?.healthy ?? 0 }}</strong>
        <small>当前未命中任何异常</small>
      </article>
      <article class="stat-card">
        <span>异常种子</span>
        <strong class="bad">{{ summary?.abnormal ?? 0 }}</strong>
        <small>待处理 {{ summary?.pending ?? 0 }} · 本次恢复 {{ summary?.recovered ?? 0 }}</small>
      </article>
      <article class="stat-card">
        <span>H&amp;R 风险</span>
        <strong class="warn">
          {{ summary?.hnr_risk ? summary.hnr_risk : '—' }}
        </strong>
        <small>按站点规则估算，规则未知时不计入</small>
      </article>
    </div>

    <p v-if="summary" class="coverage-note">
      本轮检查覆盖：文件 {{ summary.coverage.files }} 个 · Tracker {{ summary.coverage.tracker }} 个
      · 站点侧确认 {{ summary.coverage.site }} 个（抽样轮转，默认关闭）；已关联站点的种子
      {{ summary.coverage.identity }} 个。
    </p>

    <nav class="kind-tabs" aria-label="异常类型">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        :class="{ active: activeTab === tab.key }"
        @click="selectTab(tab.key)"
      >
        {{ tab.label }}
        <span class="tab-count">{{ tab.count }}</span>
      </button>
    </nav>

    <div class="filter-bar">
      <label class="search-field">
        <Search :size="16" />
        <input
          v-model="search"
          type="search"
          placeholder="搜索种子名称"
          aria-label="搜索种子名称"
          @keydown.enter="applyFilters"
        />
      </label>
      <el-select v-model="siteFilter" placeholder="全部站点" clearable @change="applyFilters">
        <el-option label="全部站点" value="" />
        <el-option
          v-for="site in sites"
          :key="site.id"
          :label="site.name"
          :value="site.type.toLowerCase()"
        />
      </el-select>
      <el-select
        v-model="downloaderFilter"
        placeholder="全部下载器"
        clearable
        @change="applyFilters"
      >
        <el-option label="全部下载器" value="" />
        <el-option
          v-for="downloader in downloaders"
          :key="downloader.id"
          :label="downloader.name"
          :value="downloader.id"
        />
      </el-select>
      <el-select v-model="severityFilter" placeholder="全部风险" clearable @change="applyFilters">
        <el-option label="全部风险" value="" />
        <el-option label="高风险" value="HIGH" />
        <el-option label="中风险" value="MEDIUM" />
        <el-option label="低风险" value="LOW" />
      </el-select>
      <label class="switch-field">
        <el-switch v-model="showResolved" @change="applyFilters" />
        <span>显示已恢复</span>
      </label>
      <button class="ghost-button" type="button" @click="resetFilters">重置</button>
    </div>

    <div class="table-card">
      <el-table :data="items" v-loading="loading" row-key="id" empty-text=" ">
        <el-table-column label="种子 / 影片" min-width="240">
          <template #default="{ row }">
            <div class="torrent-cell">
              <b>{{ row.torrent_name }}</b>
              <small>{{ formatBytes(row.size_bytes) }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="站点" width="130">
          <template #default="{ row }">
            <span :class="{ muted: !row.site_key }">{{ siteLabel(row.site_key) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="下载器" width="130">
          <template #default="{ row }">{{ downloaderLabel(row.downloader_id) }}</template>
        </el-table-column>
        <el-table-column label="异常类型" width="140">
          <template #default="{ row }">{{ kindLabel(row.kind) }}</template>
        </el-table-column>
        <el-table-column label="风险" width="100">
          <template #default="{ row }">
            <el-tag :type="severityTone(row.severity)" disable-transitions>
              {{ severityLabel(row.severity) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近证据" min-width="220">
          <template #default="{ row }">
            <span class="evidence" :title="evidenceSummary(row)">{{ evidenceSummary(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="发现时间" width="170">
          <template #default="{ row }">{{ formatTime(row.first_detected_at) }}</template>
        </el-table-column>
        <el-table-column label="建议动作" width="140">
          <template #default="{ row }">{{ suggestionLabel(row.suggestion) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="150" align="center">
          <template #default="{ row }">
            <el-button link type="primary" @click="openDetail(row)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="hasNeverRun && !items.length" class="empty-state">
        <Activity :size="28" />
        <p>还没有巡查记录</p>
        <small>点击右上角「立即巡查」执行第一次检查，或等待后台按间隔自动运行。</small>
      </div>
      <div v-else-if="isEmptyAfterRun && !loading" class="empty-state">
        <p>当前筛选条件下没有异常</p>
        <small>所有种子状态正常，或已被标记为忽略。</small>
      </div>

      <footer class="table-footer">
        <span>共 {{ total }} 条</span>
        <el-pagination
          layout="prev, pager, next"
          :total="total"
          :page-size="pageSize"
          :current-page="page"
          @current-change="
            (value: number) => {
              page = value;
              void loadIssues();
            }
          "
        />
      </footer>
    </div>

    <el-dialog
      v-model="settingsDialogVisible"
      title="巡查设置"
      width="min(680px, 95vw)"
      append-to-body
    >
      <section class="settings-block">
        <h3>站点侧核对</h3>
        <div class="setting-row">
          <el-switch
            :model-value="settings.site_probe_enabled"
            :loading="settingsSaving"
            @change="saveSiteProbeEnabled"
          />
          <span
            >开启后按各站点请求间隔抽样核对详情页，大库约数天覆盖一轮；关闭时只检查本地文件与
            Tracker。站点 Cookie 失效时会自动降级，不会误报删种。</span
          >
        </div>
      </section>
      <el-divider />
      <section class="settings-block">
        <h3>站点 H&amp;R 规则</h3>
        <p>留空表示规则未知，不计入 H&amp;R 风险；填写后下一轮巡查会重算风险分。</p>
        <el-table :data="policyRows" size="small" max-height="360">
          <el-table-column prop="name" label="站点" min-width="140" show-overflow-tooltip />
          <el-table-column label="最小做种时长（小时）" min-width="180">
            <template #default="{ row }">
              <el-input-number
                v-model="row.value"
                :min="0"
                :max="8760"
                :controls="false"
                class="policy-input"
              />
            </template>
          </el-table-column>
          <el-table-column label="操作" width="90">
            <template #default="{ row }">
              <el-button link type="primary" :loading="row.saving" @click="savePolicy(row)"
                >保存</el-button
              >
            </template>
          </el-table-column>
        </el-table>
      </section>
    </el-dialog>

    <SeedingHealthDetailDrawer
      v-model:id="detailId"
      :site-label="siteLabel"
      :downloader-label="downloaderLabel"
      @changed="onDetailChanged"
      @open-sites="emit('navigate', '配置中心/站点配置')"
    />
  </section>
</template>

<style scoped>
.seeding-health {
  display: grid;
  gap: 16px;
}
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.page-head h1 {
  margin: 0 0 4px;
  font-size: 22px;
}
.subtitle {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
}
.head-actions {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
.run-meta {
  display: grid;
  grid-template-columns: auto auto;
  gap: 2px 8px;
  font-size: 12px;
  color: var(--muted);
}
.run-meta b {
  color: var(--ink);
  font-weight: 500;
}
.primary-button,
.ghost-button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border-radius: 8px;
  padding: 8px 14px;
  font-size: 13px;
  cursor: pointer;
}
.primary-button {
  border: 1px solid var(--blue);
  background: var(--blue);
  color: #fff;
}
.primary-button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.ghost-button {
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--ink);
}
.banner {
  margin: 0;
  padding: 10px 14px;
  border-radius: 10px;
  font-size: 13px;
  border: 1px solid var(--line);
}
.banner.warning {
  background: #fff8e6;
  border-color: #f0d9a8;
  color: #7a5a10;
}
.banner.danger {
  background: #fdecec;
  border-color: #f3c2c2;
  color: #8a2222;
}
.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}
.stat-card {
  display: grid;
  gap: 4px;
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface);
}
.stat-card span {
  font-size: 12px;
  color: var(--muted);
}
.stat-card strong {
  font-size: 26px;
  font-weight: 500;
  line-height: 1.2;
}
.stat-card strong.ok {
  color: #2f7d32;
}
.stat-card strong.bad {
  color: #b3261e;
}
.stat-card strong.warn {
  color: #a8620a;
}
.stat-card small {
  font-size: 11px;
  color: var(--muted);
}
.settings-block h3 {
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 600;
}
.settings-block p {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--muted);
}
.setting-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  font-size: 13px;
  line-height: 1.7;
}
.policy-input {
  width: 100%;
}
.coverage-note {
  margin: 0;
  font-size: 12px;
  color: var(--muted);
  line-height: 1.7;
}
.kind-tabs {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.kind-tabs button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface);
  color: var(--ink);
  font-size: 13px;
  cursor: pointer;
}
.kind-tabs button.active {
  border-color: var(--blue);
  background: var(--blue-soft);
  color: var(--blue);
}
.tab-count {
  font-size: 12px;
  color: var(--muted);
}
.kind-tabs button.active .tab-count {
  color: var(--blue);
}
.filter-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
/* Element Plus 的下拉默认撑满容器，会把筛选栏挤成竖排。 */
.filter-bar :deep(.el-select) {
  width: 148px;
  flex: 0 0 148px;
}
.filter-bar > * {
  flex: 0 0 auto;
}
.search-field {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0 10px;
  height: 32px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  color: var(--muted);
}
.search-field input {
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--ink);
  font-size: 13px;
  width: 180px;
}
.switch-field {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--muted);
}
.table-card {
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface);
  overflow: hidden;
}
.torrent-cell {
  display: grid;
  gap: 2px;
}
.torrent-cell b {
  font-weight: 500;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.torrent-cell small {
  color: var(--muted);
  font-size: 11px;
}
.evidence {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--muted);
}
.muted {
  color: var(--muted);
}
.table-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border-top: 1px solid var(--line);
  font-size: 12px;
  color: var(--muted);
}
.empty-state {
  display: grid;
  justify-items: center;
  gap: 6px;
  padding: 36px 16px;
  color: var(--muted);
}
.empty-state p {
  margin: 0;
  color: var(--ink);
  font-size: 14px;
}
.empty-state small {
  font-size: 12px;
}
.spinning {
  animation: spin 1s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
@media (max-width: 900px) {
  .stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
