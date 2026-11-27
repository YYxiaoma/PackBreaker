<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { Download, RefreshCw } from '@lucide/vue';
import { ElMessage } from 'element-plus';
import {
  exportCoverageCsv,
  getCoverageScope,
  getCoverageSummary,
  listCoverageMovies,
  listCoverageRuns,
  saveCoverageScope,
  startCoverageRun,
  type CoverageMovie,
  type CoverageSummary,
} from '../api/mediaCoverage';
import { browseUnpackTree, listUnpackTreeRoots } from '../api/unpack';
import { toApiProblem } from '../api/client';

const emit = defineEmits<{ navigate: [route: '监控中心/种子收益分析'] }>();

const bucketLabels: Record<string, string> = {
  ZERO: '完全没做种',
  ONE: '只在 1 个站',
  TWO: '已在 2 个站',
  THREE_PLUS: '已在 3 站以上',
  UNKNOWN: '站点未知 / 低置信',
};
const suggestionLabels: Record<string, string> = {
  ADD_SEED: '去补种',
  REVIEW_LOW_YIELD: '查看低收益候选',
  CONFIRM_IDENTITY: '需确认身份',
  KEEP: '保持',
};
const confidenceLabels: Record<string, string> = {
  HIGH: '高',
  MEDIUM: '中',
  LOW: '低',
};

interface TreeNodeData {
  name: string;
  path: string;
  token: string;
}

const summary = ref<CoverageSummary | null>(null);
const movies = ref<CoverageMovie[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(20);
const activeBucket = ref<string | null>('ONE');
const query = ref('');
const resolution = ref('');
const loading = ref(false);
const scanning = ref(false);
const exporting = ref(false);
const errorText = ref('');

const scopeToken = ref('');
const scopeLabel = ref('');
const directoryPickerVisible = ref(false);
const selectedTreeNode = ref<TreeNodeData | null>(null);
let pollTimer: ReturnType<typeof setInterval> | undefined;

const buckets = computed(() => {
  const values = summary.value?.buckets ?? {};
  return ['ZERO', 'ONE', 'TWO', 'THREE_PLUS', 'UNKNOWN'].map((key) => ({
    key,
    label: bucketLabels[key],
    count: values[key] ?? 0,
  }));
});
const maxBucket = computed(() => Math.max(1, ...buckets.value.map((item) => item.count)));
const lastRun = computed(() => summary.value?.last_run ?? null);
const isPolling = computed(() => scanning.value);

function formatNumber(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? '—'
    : value.toLocaleString('zh-CN');
}

function parseCsv(text: string): string[][] {
  const source = text.replace(/^\ufeff/, '');
  const rows: string[][] = [];
  let row: string[] = [];
  let field = '';
  let quoted = false;
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (quoted) {
      if (char === '"' && source[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        field += char;
      }
      continue;
    }
    if (char === '"') quoted = true;
    else if (char === ',') {
      row.push(field);
      field = '';
    } else if (char === '\r') {
      continue;
    } else if (char === '\n') {
      row.push(field);
      rows.push(row);
      row = [];
      field = '';
    } else {
      field += char;
    }
  }
  if (field || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

function serializeCsv(rows: string[][]): string {
  const body = rows
    .map((row) =>
      row
        .map((value) => (/[",\r\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value))
        .join(','),
    )
    .join('\r\n');
  return `\ufeff${body}\r\n`;
}

async function loadSummary(): Promise<void> {
  summary.value = await getCoverageSummary();
}

async function loadMovies(): Promise<void> {
  const result = await listCoverageMovies(activeBucket.value, {
    query: query.value,
    resolution: resolution.value,
    page: page.value,
    pageSize: pageSize.value,
  });
  movies.value = result.items;
  total.value = result.total;
}

async function load(): Promise<void> {
  loading.value = true;
  errorText.value = '';
  try {
    await Promise.all([loadSummary(), loadMovies()]);
  } catch (error) {
    errorText.value = toApiProblem(error).message;
  } finally {
    loading.value = false;
  }
}

function openDirectoryPicker(): void {
  selectedTreeNode.value = null;
  directoryPickerVisible.value = true;
}

async function loadTreeNode(
  node: { level: number; data?: TreeNodeData },
  resolve: (data: TreeNodeData[]) => void,
): Promise<void> {
  try {
    const entries =
      node.level === 0
        ? await listUnpackTreeRoots()
        : (await browseUnpackTree(node.data?.token ?? '')).entries;
    resolve(
      entries.map((entry) => ({
        name: entry.name,
        path: entry.display_path,
        token: entry.selection_token,
      })),
    );
  } catch (error) {
    ElMessage.error(toApiProblem(error).message);
    resolve([]);
  }
}

function onTreeNodeClick(data: TreeNodeData): void {
  selectedTreeNode.value = data;
}

async function confirmDirectorySelection(): Promise<void> {
  if (!selectedTreeNode.value) {
    ElMessage.warning('请先选择目录节点');
    return;
  }
  try {
    await saveCoverageScope(selectedTreeNode.value.token, selectedTreeNode.value.path);
    scopeToken.value = selectedTreeNode.value.token;
    scopeLabel.value = selectedTreeNode.value.path;
    directoryPickerVisible.value = false;
    ElMessage.success('扫描目录已保存，开始扫描');
    await scan();
  } catch (error) {
    ElMessage.error(toApiProblem(error).message);
  }
}

function stopPolling(): void {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = undefined;
}

// 开启站点列表探测后，一次全库扫描实测要十分钟上下；轮询预算给到 20 分钟，
// 超预算只提示「仍在后台运行」，不再谎报扫描失败。
const POLL_INTERVAL_MS = 3000;
const MAX_POLL_ATTEMPTS = 400;

async function pollRun(runId: string): Promise<void> {
  stopPolling();
  let attempts = 0;
  pollTimer = setInterval(async () => {
    attempts += 1;
    try {
      const runs = await listCoverageRuns(10);
      const run = runs.find((item) => item.id === runId);
      if (!run) return;
      if (run.status === 'COMPLETED') {
        stopPolling();
        scanning.value = false;
        ElMessage.success('覆盖率扫描完成');
        await load();
      } else if (run.status === 'FAILED') {
        stopPolling();
        scanning.value = false;
        ElMessage.error(run.error_code ? `扫描失败：${run.error_code}` : '扫描失败');
        await loadSummary();
      } else if (attempts >= MAX_POLL_ATTEMPTS) {
        stopPolling();
        scanning.value = false;
        ElMessage.warning('扫描仍在后台运行，稍后刷新即可看到结果');
        await loadSummary();
      }
    } catch (error) {
      stopPolling();
      scanning.value = false;
      ElMessage.error(toApiProblem(error).message);
    }
  }, POLL_INTERVAL_MS);
}

async function scan(): Promise<void> {
  if (!scopeToken.value) {
    ElMessage.warning('请先选择本地库目录');
    return;
  }
  scanning.value = true;
  try {
    const runId = await startCoverageRun(scopeToken.value);
    await pollRun(runId);
  } catch (error) {
    scanning.value = false;
    ElMessage.error(toApiProblem(error).message);
  }
}

function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

async function exportB1(): Promise<void> {
  exporting.value = true;
  try {
    const zero = await exportCoverageCsv('ZERO');
    const one = await exportCoverageCsv('ONE');
    const zeroRows = parseCsv(await zero.blob.text());
    const oneRows = parseCsv(await one.blob.text());
    const merged = zeroRows.length ? [...zeroRows, ...oneRows.slice(1)] : oneRows;
    downloadBlob(
      new Blob([serializeCsv(merged)], { type: 'text/csv;charset=utf-8' }),
      'b1-seed-candidates.csv',
    );
    ElMessage.success('补种清单已导出');
  } catch (error) {
    ElMessage.error(toApiProblem(error).message);
  } finally {
    exporting.value = false;
  }
}

async function exportCurrent(): Promise<void> {
  exporting.value = true;
  try {
    const { blob, filename } = await exportCoverageCsv(activeBucket.value);
    downloadBlob(blob, filename);
  } catch (error) {
    ElMessage.error(toApiProblem(error).message);
  } finally {
    exporting.value = false;
  }
}

function selectBucket(key: string | null): void {
  activeBucket.value = key;
  page.value = 1;
  void loadMovies();
}

onMounted(async () => {
  try {
    const scope = await getCoverageScope();
    if (scope) {
      scopeToken.value = scope.selection_token;
      scopeLabel.value = scope.scope_label;
    }
  } catch (error) {
    errorText.value = toApiProblem(error).message;
  }
  await load();
});

onBeforeUnmount(stopPolling);
</script>

<template>
  <section class="coverage-panel">
    <header class="page-head">
      <div>
        <h1>辅种覆盖情况</h1>
        <p>扫描本地库，找出缺少辅种与已多站覆盖的影片</p>
      </div>
      <div class="head-actions">
        <span v-if="scopeLabel" class="scope-path" :title="scopeLabel">{{ scopeLabel }}</span>
        <el-button @click="openDirectoryPicker">选择目录</el-button>
        <el-button :loading="exporting" @click="exportB1">
          <Download :size="15" /> 导出补种清单
        </el-button>
        <el-button :loading="scanning" type="primary" @click="scan">
          <RefreshCw :size="15" /> 重新扫描
        </el-button>
      </div>
    </header>

    <div v-if="errorText" class="banner danger" role="alert">{{ errorText }}</div>

    <p class="run-line">
      <template v-if="lastRun">
        上次扫描 {{ lastRun.scope_label }} · {{ lastRun.status }} ·
        {{ lastRun.movie_count }} 个影片文件（同一影片的多版本按文件分别统计）
        <span v-if="isPolling">（扫描中）</span>
      </template>
      <template v-else>尚无扫描记录，请先选择目录并开始扫描。</template>
    </p>

    <div class="stat-grid">
      <article>
        <span>只在 1 个站做种</span>
        <strong>{{ formatNumber(summary?.buckets?.ONE) }}</strong>
      </article>
      <article>
        <span>完全没做种</span>
        <strong>{{ formatNumber(summary?.buckets?.ZERO) }}</strong>
      </article>
      <article>
        <span>已在 3 站以上做种</span>
        <strong>{{ formatNumber(summary?.buckets?.THREE_PLUS) }}</strong>
      </article>
      <article>
        <span>本地影片总数</span>
        <strong>{{ formatNumber(summary?.total_movies) }}</strong>
      </article>
    </div>

    <div v-if="summary && summary.low_confidence_count" class="banner warning" role="status">
      {{ summary.low_confidence_count }} 部影片身份置信度低，已单列，不计入“完全没做种”。
    </div>
    <div v-if="lastRun?.degraded_sites.length" class="banner warning" role="status">
      站点观测不完整：{{ lastRun.degraded_sites.join('、') }}
    </div>

    <section class="distribution-card">
      <div class="card-title">
        <h2>覆盖率分布</h2>
        <span>点击分桶筛选下方列表</span>
      </div>
      <button
        v-for="item in buckets"
        :key="item.key"
        type="button"
        class="bucket-row"
        :class="{ active: activeBucket === item.key }"
        @click="selectBucket(item.key)"
      >
        <span class="bucket-label">{{ item.label }}</span>
        <span class="bucket-bar">
          <i :style="{ width: `${(item.count / maxBucket) * 100}%` }"></i>
        </span>
        <strong>{{ item.count }}</strong>
      </button>
    </section>

    <section class="table-card">
      <div class="filter-bar">
        <el-radio-group :model-value="activeBucket ?? ''" @change="selectBucket($event || null)">
          <el-radio-button value="">全部</el-radio-button>
          <el-radio-button value="ZERO">没做种</el-radio-button>
          <el-radio-button value="ONE">1 站</el-radio-button>
          <el-radio-button value="THREE_PLUS">3+ 站</el-radio-button>
        </el-radio-group>
        <el-input
          v-model="query"
          placeholder="搜索影片"
          clearable
          class="wide"
          @keyup.enter="
            page = 1;
            loadMovies();
          "
        />
        <el-button
          @click="
            page = 1;
            loadMovies();
          "
          >查询</el-button
        >
        <el-button @click="exportCurrent">导出当前分桶</el-button>
      </div>
      <div class="table-scroll">
        <el-table :data="movies" v-loading="loading" row-key="movie_key" empty-text=" ">
          <el-table-column label="影片" min-width="220">
            <template #default="{ row }">
              <strong>{{ row.display_title }}</strong>
              <small>{{ row.year || '—' }} · {{ row.resolution || '未知分辨率' }}</small>
            </template>
          </el-table-column>
          <el-table-column label="做种站点" min-width="170">
            <template #default="{ row }">{{
              row.site_keys.length ? row.site_keys.join('、') : '—'
            }}</template>
          </el-table-column>
          <el-table-column label="覆盖站数" width="100">
            <template #default="{ row }">{{ row.coverage_count }}</template>
          </el-table-column>
          <el-table-column label="建议" min-width="160">
            <template #default="{ row }">
              <el-button
                v-if="row.suggestion === 'REVIEW_LOW_YIELD'"
                link
                type="primary"
                @click="emit('navigate', '监控中心/种子收益分析')"
              >
                {{ suggestionLabels[row.suggestion] }}
              </el-button>
              <span v-else>{{ suggestionLabels[row.suggestion] || row.suggestion }}</span>
            </template>
          </el-table-column>
          <el-table-column label="置信度" width="90">
            <template #default="{ row }">{{
              confidenceLabels[row.confidence] || row.confidence
            }}</template>
          </el-table-column>
          <el-table-column label="相对路径" min-width="220">
            <template #default="{ row }">
              <span :title="row.relative_path_hint || ''">{{ row.relative_path_hint || '—' }}</span>
            </template>
          </el-table-column>
        </el-table>
      </div>
      <p v-if="!movies.length && !loading" class="empty">当前分桶没有影片。</p>
      <div class="pagination">
        <el-pagination
          layout="prev, pager, next"
          :total="total"
          :page-size="pageSize"
          :current-page="page"
          @current-change="
            page = $event;
            loadMovies();
          "
        />
      </div>
    </section>

    <el-dialog v-model="directoryPickerVisible" width="min(850px, 95vw)">
      <template #header>
        <div class="directory-picker-header">
          <span>选择目录</span>
          <div class="directory-picker-header-actions">
            <el-button size="small" @click="confirmDirectorySelection">使用此目录</el-button>
            <el-button size="small" @click="directoryPickerVisible = false">取消</el-button>
          </div>
        </div>
      </template>
      <div class="tree-picker">
        <div class="tree-picker-tree">
          <el-tree
            lazy
            :load="loadTreeNode"
            node-key="token"
            :props="{ label: 'name', isLeaf: () => false }"
            highlight-current
            :expand-on-click-node="false"
            @node-click="onTreeNodeClick"
          />
        </div>
        <aside>
          <b>当前选择</b>
          <p>{{ selectedTreeNode?.path || '请选择目录节点' }}</p>
          <small>确认后将保存到后端并立即开始扫描，每晚 05:00 自动重扫。</small>
        </aside>
      </div>
    </el-dialog>
  </section>
</template>

<style scoped>
.coverage-panel {
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
  justify-content: space-between;
  gap: 20px;
  align-items: flex-start;
}
h1 {
  margin: 0;
  font-size: 25px;
  font-weight: 600;
}
.page-head p,
.card-title span,
.run-line {
  color: var(--muted);
  font-size: 13px;
}
.page-head p {
  margin: 6px 0 0;
}
.head-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  justify-content: flex-end;
}
.banner {
  border-radius: 8px;
  padding: 10px 14px;
  font-size: 13px;
}
.banner.warning {
  background: var(--warning-soft, #fff7e6);
  color: var(--warning, #9a6700);
}
.banner.danger {
  background: var(--danger-soft, #fef0f0);
  color: var(--danger, #b42318);
}
.distribution-card,
.table-card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 16px 18px;
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
.card-title {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: baseline;
}
.card-title h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}
.scope-path {
  max-width: 280px;
  min-width: 0;
  color: var(--muted);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.directory-picker-header {
  display: flex;
  align-items: center;
  gap: 14px;
}
.directory-picker-header > span {
  font-weight: 600;
}
.directory-picker-header-actions {
  display: flex;
  gap: 8px;
}
.tree-picker {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 280px;
  gap: 16px;
  min-height: 420px;
}
.tree-picker-tree {
  min-height: 380px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  overflow: auto;
}
.tree-picker-tree :deep(.el-tree) {
  padding: 0;
  border: 0;
}
.tree-picker aside {
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--canvas);
}
.tree-picker aside p {
  overflow-wrap: anywhere;
  font-size: 13px;
}
.tree-picker aside small {
  color: var(--muted);
  line-height: 1.6;
}
.run-line {
  margin: 0;
}
.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}
.stat-grid article {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 16px 18px;
  display: grid;
  gap: 6px;
}
.stat-grid span {
  color: var(--muted);
  font-size: 13px;
}
.stat-grid strong {
  font-size: 24px;
  font-weight: 600;
}
.bucket-row {
  display: grid;
  grid-template-columns: 130px 1fr 60px;
  gap: 12px;
  align-items: center;
  width: 100%;
  border: 0;
  background: transparent;
  padding: 8px 6px;
  cursor: pointer;
  color: inherit;
  text-align: left;
  font-size: 13px;
}
.bucket-row.active {
  background: var(--blue-soft);
  border-radius: 8px;
}
.bucket-bar {
  height: 10px;
  background: var(--line);
  border-radius: 999px;
  overflow: hidden;
}
.bucket-bar i {
  display: block;
  height: 100%;
  background: #378add;
}
.filter-bar {
  display: flex;
  gap: 10px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.wide {
  width: 240px;
}
.el-table-column--default small {
  display: block;
  color: var(--muted);
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
@media (max-width: 900px) {
  .stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .page-head,
  .scope-head {
    flex-direction: column;
  }
}
</style>
