<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { Download, RefreshCw } from '@lucide/vue';
import { ElMessage } from 'element-plus';
import {
  exportCleanupCsv,
  getYieldSummary,
  listYieldTorrents,
  previewCleanup,
  refreshYield,
  type CleanupPreview,
  type YieldSummary,
  type YieldTorrent,
} from '../api/torrentYield';
import { toApiProblem } from '../api/client';

const days = ref(30);
const sort = ref<'upload' | 'bonus' | 'hourly'>('upload');
const order = ref<'asc' | 'desc'>('desc');
const zeroOnly = ref(false);
const query = ref('');
const site = ref('');
const page = ref(1);
const pageSize = ref(20);
const total = ref(0);
const loading = ref(false);
const refreshing = ref(false);
const previewing = ref(false);
const exporting = ref(false);
const errorText = ref('');
const summary = ref<YieldSummary | null>(null);
const rows = ref<YieldTorrent[]>([]);
const selected = ref<string[]>([]);
const preview = ref<CleanupPreview | null>(null);

function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
  let scaled = value / 1024;
  let unit = 0;
  while (scaled >= 1024 && unit < units.length - 1) {
    scaled /= 1024;
    unit += 1;
  }
  return `${scaled.toFixed(scaled >= 100 ? 0 : 1)} ${units[unit]}`;
}

function formatNumber(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? '—'
    : value.toLocaleString('zh-CN', { maximumFractionDigits: digits });
}

function formatDays(seconds: number): string {
  return `${(seconds / 86400).toFixed(1)} 天`;
}

function statusLabel(row: YieldTorrent): string {
  if (row.zero_upload) return '零上传';
  if (row.status === 'REBASELINE') return '计数重置';
  if (row.status === 'INCOMPLETE') return '样本不足';
  return '正常';
}

async function loadRows(): Promise<void> {
  try {
    const result = await listYieldTorrents(days.value, {
      sort: sort.value,
      order: order.value,
      zeroOnly: zeroOnly.value,
      query: query.value,
      site: site.value,
      page: page.value,
      pageSize: pageSize.value,
    });
    rows.value = result.items;
    total.value = result.total;
  } catch (error) {
    errorText.value = toApiProblem(error).message;
  }
}

async function load(): Promise<void> {
  loading.value = true;
  errorText.value = '';
  try {
    const nextSummary = await getYieldSummary(days.value);
    summary.value = nextSummary;
    await loadRows();
  } catch (error) {
    errorText.value = toApiProblem(error).message;
  } finally {
    loading.value = false;
  }
}

async function refresh(): Promise<void> {
  refreshing.value = true;
  try {
    const result = await refreshYield();
    if (!result.accepted && result.reason === 'REFRESH_TOO_SOON') {
      ElMessage.warning('刷新过频，请稍后再试');
    } else {
      ElMessage.success(`已采样 ${result.recorded} 个种子`);
      await load();
    }
  } catch (error) {
    ElMessage.error(toApiProblem(error).message);
  } finally {
    refreshing.value = false;
  }
}

async function simulate(): Promise<void> {
  if (!selected.value.length) {
    ElMessage.warning('请先选择种子');
    return;
  }
  previewing.value = true;
  try {
    preview.value = await previewCleanup(selected.value);
  } catch (error) {
    ElMessage.error(toApiProblem(error).message);
  } finally {
    previewing.value = false;
  }
}

async function exportCsv(): Promise<void> {
  if (!selected.value.length) {
    ElMessage.warning('请先选择种子');
    return;
  }
  exporting.value = true;
  try {
    const { blob, filename } = await exportCleanupCsv(selected.value);
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    URL.revokeObjectURL(url);
    ElMessage.success('清理清单已导出');
  } catch (error) {
    ElMessage.error(toApiProblem(error).message);
  } finally {
    exporting.value = false;
  }
}

onMounted(load);
</script>

<template>
  <section class="yield-panel">
    <header class="page-head">
      <div>
        <h1>种子收益分析</h1>
        <p>上传量为下载器实测；积分与时魔仅在站点提供真实单种子贡献时显示</p>
      </div>
      <div class="head-actions">
        <div class="window-switch" role="group" aria-label="时间窗口">
          <button
            v-for="option in [7, 30, 90]"
            :key="option"
            type="button"
            :class="{ active: days === option }"
            @click="
              days = option;
              load();
            "
          >
            {{ option }}天
          </button>
        </div>
        <el-button :loading="refreshing" @click="refresh">
          <RefreshCw :size="15" /> 刷新数据
        </el-button>
      </div>
    </header>

    <div v-if="errorText" class="banner danger" role="alert">{{ errorText }}</div>
    <p v-if="summary && days > summary.retention_days" class="retention-hint">
      日采样只保留最近 {{ summary.retention_days }} 天，更早的日期没有数据，窗口内的 天数会小于
      {{ days }} 天
    </p>

    <div class="stat-grid">
      <article class="stat-card">
        <span>窗口上传总量（实测）</span>
        <strong>{{ formatBytes(summary?.total_upload_bytes) }}</strong>
        <small>来自下载器累计上传量增量</small>
      </article>
      <article class="stat-card">
        <span>积分贡献</span>
        <strong>
          {{
            summary?.contribution_available
              ? formatNumber(summary?.total_bonus_contribution)
              : '暂不可用'
          }}
        </strong>
        <small>站点未提供真实单种子贡献时不估算</small>
      </article>
      <article class="stat-card">
        <span>时魔贡献</span>
        <strong>
          {{
            summary?.contribution_available
              ? formatNumber(summary?.total_bonus_per_hour_contribution)
              : '暂不可用'
          }}
        </strong>
        <small>不按账户总量分摊</small>
      </article>
      <article class="stat-card">
        <span>零上传种子</span>
        <strong>{{ summary?.zero_upload_count ?? 0 }}</strong>
        <small>至少两次有效观测且做种满 7 天</small>
      </article>
    </div>

    <section class="table-card">
      <div class="filter-bar">
        <el-input
          v-model="query"
          placeholder="搜索种子名称"
          clearable
          class="wide"
          @keyup.enter="
            page = 1;
            loadRows();
          "
        />
        <el-input
          v-model="site"
          placeholder="站点 key"
          clearable
          class="narrow"
          @keyup.enter="
            page = 1;
            loadRows();
          "
        />
        <el-select v-model="sort" class="sort-select">
          <el-option label="按上传量" value="upload" />
          <el-option label="按积分贡献" value="bonus" />
          <el-option label="按时魔贡献" value="hourly" />
        </el-select>
        <el-switch
          v-model="zeroOnly"
          active-text="仅零上传"
          @change="
            page = 1;
            loadRows();
          "
        />
        <el-button
          @click="
            page = 1;
            loadRows();
          "
          >查询</el-button
        >
      </div>

      <div class="table-scroll">
        <el-table
          :data="rows"
          v-loading="loading"
          row-key="key"
          empty-text=" "
          @selection-change="selected = ($event as YieldTorrent[]).map((item) => item.key)"
        >
          <el-table-column type="selection" width="46" />
          <el-table-column label="#" width="52">
            <template #default="{ $index }">{{ (page - 1) * pageSize + $index + 1 }}</template>
          </el-table-column>
          <el-table-column label="种子" min-width="240">
            <template #default="{ row }">
              <div class="torrent-cell">
                <strong>{{ row.title }}</strong>
                <span>{{ row.torrent_hash.slice(0, 12) }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="站点" min-width="110">
            <template #default="{ row }">{{ row.site_key || '未知' }}</template>
          </el-table-column>
          <el-table-column label="上传量" min-width="120">
            <template #default="{ row }">{{ formatBytes(row.upload_bytes) }}</template>
          </el-table-column>
          <el-table-column label="积分贡献" min-width="120">
            <template #default="{ row }">{{
              row.contribution_available ? formatNumber(row.bonus_contribution) : '—'
            }}</template>
          </el-table-column>
          <el-table-column label="时魔" min-width="110">
            <template #default="{ row }">{{
              row.contribution_available ? formatNumber(row.bonus_per_hour_contribution) : '—'
            }}</template>
          </el-table-column>
          <el-table-column label="做种时长" min-width="110">
            <template #default="{ row }">{{ formatDays(row.seeding_seconds) }}</template>
          </el-table-column>
          <el-table-column label="可回收空间" min-width="130">
            <template #default="{ row }">{{
              row.reclaimable_bytes ? formatBytes(row.reclaimable_bytes) : '—'
            }}</template>
          </el-table-column>
          <el-table-column label="状态" min-width="100">
            <template #default="{ row }">
              <span :class="['status-tag', { zero: row.zero_upload }]">{{ statusLabel(row) }}</span>
            </template>
          </el-table-column>
        </el-table>
      </div>
      <p v-if="!rows.length && !loading" class="empty">
        当前窗口没有种子观测，点击“刷新数据”开始采样。
      </p>
      <div class="pagination">
        <el-pagination
          layout="prev, pager, next"
          :total="total"
          :page-size="pageSize"
          :current-page="page"
          @current-change="
            page = $event;
            loadRows();
          "
        />
      </div>
    </section>

    <section class="cleanup-card">
      <div class="cleanup-head">
        <div>
          <h2>清理决策预览</h2>
          <p>只做模拟与导出，不会删除种子或执行去重操作</p>
        </div>
        <div class="cleanup-actions">
          <el-button :loading="exporting" @click="exportCsv">
            <Download :size="15" /> 导出清单
          </el-button>
          <el-button type="primary" :loading="previewing" @click="simulate">模拟清理</el-button>
        </div>
      </div>
      <p class="selected-count">已选 {{ selected.length }} 个种子</p>
      <div v-if="preview" class="preview-grid">
        <div>
          <span>可回收空间</span>
          <strong>{{ formatBytes(preview.reclaimable_bytes) }}</strong>
        </div>
        <div>
          <span>预计积分损失</span>
          <strong>{{
            preview.bonus_loss_available ? formatNumber(preview.bonus_loss) : '暂不可用'
          }}</strong>
        </div>
        <div>
          <span>匹配到去重文件</span>
          <strong>{{ preview.matched_files }}</strong>
        </div>
      </div>
      <ul v-if="preview?.warnings.length" class="warnings">
        <li v-for="warning in preview.warnings" :key="warning">{{ warning }}</li>
      </ul>
    </section>
  </section>
</template>

<style scoped>
.yield-panel {
  display: grid;
  gap: 18px;
  width: 100%;
  min-width: 0;
  max-width: 100%;
  padding: 24px 28px 40px;
  box-sizing: border-box;
  overflow-x: hidden;
}
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
}
h1 {
  margin: 0;
  font-size: 25px;
  font-weight: 600;
}
.page-head p,
.cleanup-head p {
  margin: 6px 0 0;
  color: var(--muted);
  font-size: 13px;
}
.head-actions {
  display: flex;
  gap: 10px;
  align-items: center;
}
.window-switch {
  display: inline-flex;
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}
.window-switch button {
  border: 0;
  background: var(--surface);
  color: var(--muted);
  padding: 7px 12px;
  cursor: pointer;
  font-size: 13px;
}
.window-switch button.active {
  background: var(--blue-soft);
  color: var(--blue);
  font-weight: 600;
}
.banner {
  border-radius: 8px;
  padding: 10px 14px;
  font-size: 13px;
}
.banner.danger {
  background: var(--danger-soft, #fef0f0);
  color: var(--danger, #b42318);
}
.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}
.stat-card,
.table-card,
.cleanup-card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 12px;
  min-width: 0;
  max-width: 100%;
  box-sizing: border-box;
  overflow: hidden;
}
.table-scroll {
  width: 100%;
  min-width: 0;
  overflow-x: auto;
}
.stat-card {
  padding: 16px 18px;
  display: grid;
  gap: 6px;
}
.stat-card span {
  color: var(--muted);
  font-size: 13px;
}
.stat-card strong {
  font-size: 24px;
  font-weight: 600;
}
.stat-card small {
  color: var(--muted);
  font-size: 12px;
}
.table-card,
.cleanup-card {
  padding: 16px 18px;
}
.filter-bar {
  display: flex;
  gap: 10px;
  margin-bottom: 12px;
  flex-wrap: wrap;
  align-items: center;
}
.wide {
  width: 240px;
}
.narrow {
  width: 150px;
}
.sort-select {
  width: 140px;
}
.torrent-cell {
  display: grid;
  gap: 2px;
}
.torrent-cell span {
  color: var(--muted);
  font-size: 12px;
}
.status-tag {
  font-size: 12px;
  color: var(--muted);
}
.status-tag.zero {
  color: var(--danger, #b42318);
  font-weight: 600;
}
.empty {
  margin: 0;
  padding: 10px 0;
  color: var(--muted);
  font-size: 13px;
  text-align: center;
}
.pagination {
  display: flex;
  justify-content: flex-end;
  padding-top: 12px;
}
.retention-hint {
  margin: -4px 0 12px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.cleanup-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
}
.cleanup-head h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}
.cleanup-actions {
  display: flex;
  gap: 10px;
}
.selected-count {
  margin: 12px 0 0;
  color: var(--muted);
  font-size: 13px;
}
.preview-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
  margin-top: 12px;
}
.preview-grid div {
  background: var(--surface-soft, var(--canvas));
  border-radius: 8px;
  padding: 12px 14px;
  display: grid;
  gap: 6px;
}
.preview-grid span {
  color: var(--muted);
  font-size: 12px;
}
.preview-grid strong {
  font-size: 20px;
  font-weight: 600;
}
.warnings {
  margin: 12px 0 0;
  padding-left: 18px;
  color: var(--muted);
  font-size: 12px;
}
@media (max-width: 900px) {
  .stat-grid,
  .preview-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .page-head,
  .cleanup-head {
    flex-direction: column;
  }
}
</style>
