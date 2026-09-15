<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import {
  HardDrive,
  ShieldCheck,
  Check,
  Search,
  FolderSearch,
  Play,
  Pause,
  Download,
  RefreshCw,
  AlertTriangle,
  Server,
  Bell,
  RotateCcw,
  Plus,
} from '@lucide/vue';
import { ApiProblem } from '../api/client';
import {
  analyzeHistoryScanTasks,
  cancelHistoryScan,
  createHistoryScan,
  listHistoryScans,
  listHistoryScanTasks,
  materializeHistoryScan,
  pauseHistoryScan,
  resumeHistoryScan,
  scanHistoryBatch,
  startHistoryScan,
  type HistoryScan,
  type HistoryTaskResult,
} from '../api/historyScans';
import {
  getOperationMaintenanceReport,
  getOperationRetentionPlan,
  purgeTaskOperation,
  type OperationMaintenanceReport,
  type OperationRetentionPlan,
} from '../api/tasks';
import { createTaskActionIdempotencyKey } from '../taskActionSafety';
import DownloaderManagement from './DownloaderManagement.vue';
import SiteManagement from './SiteManagement.vue';
import ApiTokenManagement from './AutomationAccessManagement.vue';
import NotificationManagement from './NotificationManagement.vue';
import OperationalLogs from './OperationalLogs.vue';
import BackupManagement from './BackupManagement.vue';
import UpgradeCenter from './UpgradeCenter.vue';
const props = defineProps<{ page: string }>();
const emit = defineEmits<{
  navigate: [string];
}>();
const scanDialog = ref(false),
  scanPath = ref('/data/movies'),
  scanKind = ref<'影片' | '剧集'>('影片'),
  scanExclude = ref('sample, trailer, .incomplete'),
  scanTypes = ref('.mkv, .mp4, .m2ts');
const scans = ref<HistoryScan[]>([]);
const scanLoading = ref(false);
const scanActionId = ref('');
const historyTaskResults = ref<Record<string, HistoryTaskResult[]>>({});
const historyTaskSelections = ref<Record<string, HistoryTaskResult[]>>({});
const historyTaskQueries = ref<Record<string, string>>({});
const historyTaskStatusFilters = ref<
  Record<string, '' | NonNullable<HistoryTaskResult['task_status']>>
>({});
const historyMaterializationFilters = ref<
  Record<string, '' | HistoryTaskResult['materialization_status']>
>({});
const historyTaskLoadingId = ref('');
const historyAnalyzeId = ref('');
let historyRefreshTimer: ReturnType<typeof setInterval> | undefined;
function splitScanRules(value: string): string[] {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}
function replaceScan(scan: HistoryScan) {
  const index = scans.value.findIndex((item) => item.id === scan.id);
  if (index === -1) scans.value.unshift(scan);
  else scans.value[index] = scan;
}
function scanStatusLabel(scan: HistoryScan): string {
  return {
    READY: '待开始',
    SCANNING: '扫描中',
    PAUSED: '已暂停',
    CANCELLED: '已取消',
    DONE: '已完成',
  }[scan.status];
}
async function refreshHistoryScans() {
  scanLoading.value = true;
  try {
    scans.value = await listHistoryScans();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '历史扫描读取失败');
  } finally {
    scanLoading.value = false;
  }
}
async function startScan() {
  if (
    !(scanPath.value === '/data' || scanPath.value.startsWith('/data/')) ||
    scanPath.value.split('/').includes('..')
  ) {
    ElMessage.warning('请选择 /data/ 下的安全目录');
    return;
  }
  scanLoading.value = true;
  try {
    const created = await createHistoryScan({
      root_path: scanPath.value,
      media_kind: scanKind.value === '影片' ? 'MOVIE' : 'EPISODE',
      extensions: splitScanRules(scanTypes.value),
      exclude_patterns: splitScanRules(scanExclude.value),
    });
    const started = await startHistoryScan(created.id, created.version);
    replaceScan(started);
    scanDialog.value = false;
    ElMessage.success('历史扫描已创建并开始；目录读取只记录元数据，不修改媒体文件');
  } catch (error) {
    await refreshHistoryScans();
    ElMessage.error(error instanceof Error ? error.message : '历史扫描创建失败');
  } finally {
    scanLoading.value = false;
  }
}
async function advanceScan(scan: HistoryScan) {
  scanActionId.value = scan.id;
  try {
    const result = await scanHistoryBatch(scan.id, scan.version, 100);
    replaceScan(result.scan);
    ElMessage.success(
      result.has_more
        ? `本批处理 ${result.processed_count} 个文件，游标已持久化`
        : `扫描完成：新增 ${result.scan.new_count}、变化 ${result.scan.changed_count}、未变化 ${result.scan.unchanged_count}`,
    );
  } catch (error) {
    await refreshHistoryScans();
    ElMessage.error(error instanceof Error ? error.message : '历史扫描推进失败');
  } finally {
    scanActionId.value = '';
  }
}
async function toggleScan(scan: HistoryScan) {
  scanActionId.value = scan.id;
  try {
    const updated =
      scan.status === 'PAUSED'
        ? await resumeHistoryScan(scan.id, scan.version)
        : await pauseHistoryScan(scan.id, scan.version);
    replaceScan(updated);
  } catch (error) {
    await refreshHistoryScans();
    ElMessage.error(error instanceof Error ? error.message : '历史扫描状态更新失败');
  } finally {
    scanActionId.value = '';
  }
}

async function cancelScan(scan: HistoryScan) {
  try {
    await ElMessageBox.confirm(
      '取消当前历史扫描？已持久化的游标、文件快照和已生成任务都会保留；不会删除媒体文件或已有任务。',
      '取消历史扫描',
      { confirmButtonText: '取消扫描', cancelButtonText: '继续扫描', type: 'warning' },
    );
  } catch {
    return;
  }
  scanActionId.value = scan.id;
  try {
    const cancelled = await cancelHistoryScan(scan.id, scan.version);
    replaceScan(cancelled);
    ElMessage.success(`扫描已取消，游标保留在 ${cancelled.cursor ?? '起点'}`);
  } catch (error) {
    await refreshHistoryScans();
    ElMessage.error(error instanceof Error ? error.message : '历史扫描取消失败');
  } finally {
    scanActionId.value = '';
  }
}

async function restartScan(scan: HistoryScan) {
  scanActionId.value = scan.id;
  try {
    replaceScan(await startHistoryScan(scan.id, scan.version));
    ElMessage.success('新一轮增量扫描已开始；未变化文件不会重复计为新增');
  } catch (error) {
    await refreshHistoryScans();
    ElMessage.error(error instanceof Error ? error.message : '历史扫描启动失败');
  } finally {
    scanActionId.value = '';
  }
}

async function materializeScan(scan: HistoryScan) {
  scanActionId.value = scan.id;
  try {
    const result = await materializeHistoryScan(scan.id, scan.version, 100);
    replaceScan(result.scan);
    ElMessage.success(
      result.processed_count === 0
        ? '当前扫描快照已全部转换；没有重复创建任务'
        : `已处理 ${result.processed_count} 个快照：新建任务 ${result.task_created_count}、复用 ${result.task_reused_count}、跳过 ${result.skipped_count}${result.remaining_count ? `，剩余 ${result.remaining_count}` : ''}`,
    );
    if (historyTaskResults.value[scan.id]) await loadHistoryTasks(result.scan);
  } catch (error) {
    await refreshHistoryScans();
    ElMessage.error(error instanceof Error ? error.message : '历史扫描任务转换失败');
  } finally {
    scanActionId.value = '';
  }
}

async function loadHistoryTasks(scan: HistoryScan) {
  historyTaskLoadingId.value = scan.id;
  try {
    const taskStatus = historyTaskStatusFilters.value[scan.id] || undefined;
    const materializationStatus = historyMaterializationFilters.value[scan.id] || undefined;
    const query = (historyTaskQueries.value[scan.id] ?? '').trim() || undefined;
    historyTaskResults.value[scan.id] = await listHistoryScanTasks(scan.id, 500, {
      taskStatus,
      materializationStatus,
      query,
    });
    historyTaskSelections.value[scan.id] = [];
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '历史任务结果读取失败');
  } finally {
    historyTaskLoadingId.value = '';
  }
}

function setHistoryTaskSelection(scanId: string, rows: HistoryTaskResult[]) {
  historyTaskSelections.value[scanId] = rows;
}

function historyTaskSelectable(row: HistoryTaskResult): boolean {
  return row.analysis_eligible && row.task_id !== null;
}

function filteredHistoryTaskResults(scanId: string): HistoryTaskResult[] {
  const rows = historyTaskResults.value[scanId] ?? [];
  const needle = (historyTaskQueries.value[scanId] ?? '').trim().toLowerCase();
  const taskStatus = historyTaskStatusFilters.value[scanId] ?? '';
  const materializationStatus = historyMaterializationFilters.value[scanId] ?? '';
  return rows.filter((row) => {
    if (taskStatus && row.task_status !== taskStatus) return false;
    if (materializationStatus && row.materialization_status !== materializationStatus) return false;
    if (!needle) return true;
    return [
      row.relative_path,
      row.episode_label ?? '',
      row.episode_kind ?? '',
      row.task_id ?? '',
      row.task_status ?? '',
      row.task_error_code ?? '',
    ]
      .join(' ')
      .toLowerCase()
      .includes(needle);
  });
}

function historyTaskStatusType(
  status: HistoryTaskResult['task_status'],
): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  if (status === 'DONE') return 'success';
  if (status === 'RETRY' || status === 'PAUSED') return 'warning';
  if (status === 'FAILED' || status === 'CANCELLED') return 'danger';
  if (status === null) return 'info';
  return 'primary';
}

async function analyzeHistoryTaskIds(scan: HistoryScan, taskIds: string[], actionLabel: string) {
  if (taskIds.length > 10) {
    ElMessage.warning('单次批量 Analyze 最多 10 个任务，以避免站点请求风暴');
    return;
  }
  historyAnalyzeId.value = scan.id;
  try {
    const result = await analyzeHistoryScanTasks(scan.id, taskIds);
    const failures = result.items
      .filter((item) => !item.succeeded)
      .map((item) => item.error_code)
      .filter((code): code is string => code !== null);
    if (result.failed_count || result.skipped_count) {
      ElMessage.warning(
        `${actionLabel}完成：成功 ${result.succeeded_count}、失败 ${result.failed_count}、跳过 ${result.skipped_count}${failures.length ? `；${[...new Set(failures)].join(' / ')}` : ''}`,
      );
    } else {
      ElMessage.success(`${actionLabel}完成：${result.succeeded_count} 个任务已进入预演证据流程`);
    }
    await loadHistoryTasks(scan);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '历史任务批量 Analyze 失败');
  } finally {
    historyAnalyzeId.value = '';
  }
}

async function analyzeSelectedHistoryTasks(scan: HistoryScan) {
  const taskIds = (historyTaskSelections.value[scan.id] ?? [])
    .filter(historyTaskSelectable)
    .map((item) => item.task_id)
    .filter((taskId): taskId is string => taskId !== null);
  if (!taskIds.length) {
    ElMessage.warning('请选择 1 到 10 个处于 PENDING / RETRY / PAUSED 的历史任务');
    return;
  }
  await analyzeHistoryTaskIds(scan, taskIds, '批量 Analyze ');
}

async function retryHistoryTasks(scan: HistoryScan) {
  try {
    const retryable = await listHistoryScanTasks(scan.id, 11, {
      taskStatus: 'RETRY',
      materializationStatus: 'MATERIALIZED',
    });
    const taskIds = retryable
      .filter(historyTaskSelectable)
      .map((item) => item.task_id)
      .filter((taskId): taskId is string => taskId !== null)
      .slice(0, 10);
    if (!taskIds.length) {
      ElMessage.info('当前扫描没有可重试的 RETRY 历史任务');
      return;
    }
    if (retryable.length > 10) {
      ElMessage.info('RETRY 任务超过 10 个，本轮先处理最新 10 个；完成后可再次重试');
    }
    await analyzeHistoryTaskIds(scan, taskIds, 'RETRY 重试 ');
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '历史 RETRY 任务读取失败');
  }
}
const maintenanceReport = ref<OperationMaintenanceReport>();
const maintenanceLoading = ref(false);
const retentionPlan = ref<OperationRetentionPlan>();
const retentionDays = ref(30);
const retentionLoading = ref(false);
const purgingJournalId = ref('');
const retentionResultUnknown = ref(false);
const pendingRetentionPurge = ref<{
  taskId: string;
  journalId: string;
  retentionDays: number;
  idempotencyKey: string;
}>();
async function refreshMaintenanceReport() {
  maintenanceLoading.value = true;
  try {
    maintenanceReport.value = await getOperationMaintenanceReport(100);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '清理/对账报告读取失败');
  } finally {
    maintenanceLoading.value = false;
  }
}
async function refreshRetentionPlan() {
  retentionLoading.value = true;
  try {
    retentionPlan.value = await getOperationRetentionPlan(retentionDays.value, 100);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '保留期安全预览读取失败');
  } finally {
    retentionLoading.value = false;
  }
}
async function refreshMaintenanceCenter() {
  await Promise.all([refreshMaintenanceReport(), refreshRetentionPlan()]);
}
function isUnknownMutationResult(error: unknown): boolean {
  return (
    error instanceof ApiProblem &&
    (error.code === 'API_UNAVAILABLE' || error.status === 408 || (error.status ?? 0) >= 500)
  );
}
function retentionReasonLabel(reason: string): string {
  return (
    {
      ELIGIBLE: '可安全清理',
      RETENTION_WINDOW_NOT_REACHED: '未到保留期',
      TASK_NOT_TERMINAL: '任务未终态',
      TASK_CHECKPOINT_REFERENCE: '任务仍在使用该记录',
      ACTION_RECEIPT_REFERENCE: '关联操作仍在使用该记录',
      JOURNAL_REFERENCE: '其他 journal 仍引用',
    }[reason] ?? reason
  );
}
async function purgeRetentionItem(item: OperationRetentionPlan['items'][number]) {
  const planRetentionDays = retentionPlan.value?.retention_days;
  if (!item.eligible || purgingJournalId.value || planRetentionDays === undefined) return;
  try {
    await ElMessageBox.confirm(
      `清理 Journal ${item.journal_id} 的敏感 operation payload？服务端会再次验证任务终态、${planRetentionDays} 天保留期与零恢复引用。此动作不会删除媒体文件、下载器任务或其他外部资源，并会保留最小 tombstone 阻止历史幂等键再次执行。`,
      '确认清理历史操作记录',
      {
        confirmButtonText: '重新验证并清理 payload',
        cancelButtonText: '返回',
        type: 'warning',
      },
    );
  } catch {
    return;
  }
  pendingRetentionPurge.value = {
    taskId: item.task_id,
    journalId: item.journal_id,
    retentionDays: planRetentionDays,
    idempotencyKey: createTaskActionIdempotencyKey('purge', item.task_id),
  };
  retentionResultUnknown.value = false;
  await executePendingRetentionPurge();
}
async function executePendingRetentionPurge() {
  const pending = pendingRetentionPurge.value;
  if (!pending || purgingJournalId.value) return;
  purgingJournalId.value = pending.journalId;
  try {
    const result = await purgeTaskOperation(
      pending.taskId,
      pending.journalId,
      pending.retentionDays,
      pending.idempotencyKey,
    );
    pendingRetentionPurge.value = undefined;
    retentionResultUnknown.value = false;
    ElMessage.success(
      `Journal payload 已安全清理${result.idempotency_replayed ? '（幂等重放确认）' : ''}；最小 tombstone 已保留`,
    );
    await refreshMaintenanceCenter();
  } catch (error) {
    if (isUnknownMutationResult(error)) {
      retentionResultUnknown.value = true;
      ElMessage.warning('清理响应结果未知；请使用当前操作继续重试确认');
    } else {
      pendingRetentionPurge.value = undefined;
      retentionResultUnknown.value = false;
      ElMessage.error(error instanceof Error ? error.message : '历史操作记录清理失败');
      await refreshRetentionPlan();
    }
  } finally {
    purgingJournalId.value = '';
  }
}
watch(
  () => props.page,
  (page) => {
    if (page === '清理与对账') void refreshMaintenanceCenter();
    if (historyRefreshTimer !== undefined) {
      clearInterval(historyRefreshTimer);
      historyRefreshTimer = undefined;
    }
    if (page === '历史辅种') {
      void refreshHistoryScans();
      historyRefreshTimer = setInterval(() => {
        if (scans.value.some((scan) => scan.status === 'SCANNING') && !scanActionId.value) {
          void refreshHistoryScans();
        }
      }, 5000);
    }
  },
  { immediate: true },
);
onUnmounted(() => {
  if (historyRefreshTimer !== undefined) clearInterval(historyRefreshTimer);
});
const settingTab = ref('通知');
</script>
<template>
  <OperationalLogs v-if="page === '日志'" />
  <DownloaderManagement v-else-if="page === '下载器'" />
  <SiteManagement v-else-if="page === '站点管理'" />
  <div v-else-if="page === '历史辅种'">
    <div class="section-heading">
      <h2>历史目录扫描</h2>
      <el-button type="primary" @click="scanDialog = true"><Plus :size="15" />新建扫描</el-button>
    </div>
    <div class="stats mini-stats">
      <div class="stat">
        <div>扫描根目录</div>
        <strong>{{ scans.length }}</strong
        ><small>/data 目录，只读元数据扫描</small>
      </div>
      <div class="stat">
        <div>识别文件</div>
        <strong>{{ scans.reduce((a, s) => a + s.discovered_count, 0) }}</strong
        ><small>当前轮已处理文件</small>
      </div>
      <div class="stat">
        <div>新增 / 变化文件</div>
        <strong>{{ scans.reduce((a, s) => a + s.new_count + s.changed_count, 0) }}</strong
        ><small>未变化文件自动跳过</small>
      </div>
      <div class="stat">
        <div>扫描方式</div>
        <strong class="small-strong">增量扫描</strong><small>支持暂停与断点续扫</small>
      </div>
    </div>
    <el-skeleton v-if="scanLoading && scans.length === 0" :rows="3" animated />
    <article class="panel scan-card" v-for="scan in scans" :key="scan.id">
      <div class="card-title">
        <FolderSearch :size="26" />
        <div>
          <h3>{{ scan.root_path }}</h3>
          <small
            >{{ scan.id }} · {{ scan.media_kind === 'MOVIE' ? '影片' : '剧集' }} · 第
            {{ scan.generation }} 轮 · 游标 {{ scan.cursor ?? '未开始' }}</small
          >
        </div>
        <el-tag
          :type="
            scan.status === 'DONE' ? 'success' : scan.status === 'PAUSED' ? 'warning' : 'primary'
          "
          >{{ scanStatusLabel(scan) }}</el-tag
        >
      </div>
      <div class="scan-bottom">
        <span
          >已处理 {{ scan.discovered_count }} · 新增 {{ scan.new_count }} · 变化
          {{ scan.changed_count }} · 未变化 {{ scan.unchanged_count }}</span
        >
        <div>
          <el-button
            v-if="scan.status === 'SCANNING'"
            size="small"
            :loading="scanActionId === scan.id"
            @click="advanceScan(scan)"
            >推进扫描</el-button
          ><el-button
            v-if="scan.status === 'SCANNING' || scan.status === 'PAUSED'"
            size="small"
            :disabled="scanActionId === scan.id"
            @click="toggleScan(scan)"
            >{{ scan.status === 'PAUSED' ? '断点续扫' : '暂停' }}</el-button
          ><el-button
            v-if="scan.status === 'SCANNING' || scan.status === 'PAUSED'"
            size="small"
            type="danger"
            plain
            :disabled="scanActionId === scan.id"
            @click="cancelScan(scan)"
            >取消扫描</el-button
          ><el-button
            v-if="scan.status === 'DONE'"
            size="small"
            type="success"
            plain
            :loading="scanActionId === scan.id"
            @click="materializeScan(scan)"
            >生成 / 同步任务</el-button
          ><el-button
            size="small"
            :loading="historyTaskLoadingId === scan.id"
            @click="loadHistoryTasks(scan)"
            >任务结果</el-button
          ><el-button
            v-if="['READY', 'DONE', 'CANCELLED'].includes(scan.status)"
            size="small"
            type="primary"
            plain
            :loading="scanActionId === scan.id"
            @click="restartScan(scan)"
            >{{ scan.status === 'READY' ? '开始扫描' : '开始新一轮扫描' }}</el-button
          >
        </div>
      </div>
      <div v-if="historyTaskResults[scan.id]" class="section-space">
        <div class="filters section-space">
          <el-input
            v-model="historyTaskQueries[scan.id]"
            clearable
            placeholder="筛选路径、S01E01、任务 ID、错误码…"
            @keyup.enter="loadHistoryTasks(scan)"
          >
            <template #prefix><Search :size="15" /></template>
          </el-input>
          <el-select v-model="historyTaskStatusFilters[scan.id]" placeholder="任务状态" clearable>
            <el-option label="PENDING" value="PENDING" />
            <el-option label="RETRY" value="RETRY" />
            <el-option label="PAUSED" value="PAUSED" />
            <el-option label="DONE" value="DONE" />
            <el-option label="FAILED" value="FAILED" />
            <el-option label="CANCELLED" value="CANCELLED" />
          </el-select>
          <el-select
            v-model="historyMaterializationFilters[scan.id]"
            placeholder="转换结果"
            clearable
          >
            <el-option label="MATERIALIZED" value="MATERIALIZED" />
            <el-option label="SKIPPED" value="SKIPPED" />
          </el-select>
          <el-button :loading="historyTaskLoadingId === scan.id" @click="loadHistoryTasks(scan)"
            >应用筛选</el-button
          >
          <el-button
            type="primary"
            :loading="historyAnalyzeId === scan.id"
            :disabled="!(historyTaskSelections[scan.id]?.length ?? 0)"
            @click="analyzeSelectedHistoryTasks(scan)"
            ><Search :size="15" />批量分析
            <span v-if="historyTaskSelections[scan.id]?.length"
              >({{ historyTaskSelections[scan.id]?.length }})</span
            ></el-button
          >
          <el-button
            type="warning"
            plain
            :loading="historyAnalyzeId === scan.id"
            @click="retryHistoryTasks(scan)"
            >重试 RETRY</el-button
          >
          <el-button @click="emit('navigate', '预演与确认')">打开审核中心</el-button>
          <span class="muted">每批最多处理 10 个任务。</span>
        </div>
        <el-table
          :data="filteredHistoryTaskResults(scan.id)"
          row-key="materialization_id"
          @selection-change="setHistoryTaskSelection(scan.id, $event)"
          empty-text="当前扫描尚未生成任务转换记录"
        >
          <el-table-column type="selection" width="44" :selectable="historyTaskSelectable" />
          <el-table-column label="文件 / 单元" min-width="260">
            <template #default="{ row }">
              <div class="task-title">
                <div>
                  <b>{{ row.relative_path }}</b>
                  <span v-if="row.episode_label">
                    <el-tag size="small" type="info">{{ row.episode_label }}</el-tag>
                    <small v-if="row.variant_count > 1">同集 {{ row.variant_count }} 个版本</small>
                  </span>
                  <small
                    >{{ row.unit_kind ?? '未识别单元' }} ·
                    {{ row.normalized_unit_key ?? '—' }}</small
                  >
                </div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="转换结果" width="180">
            <template #default="{ row }">
              <el-tag :type="row.materialization_status === 'MATERIALIZED' ? 'success' : 'info'">
                {{ row.materialization_status }}
              </el-tag>
              <small v-if="row.reason_code" class="cell-note">{{ row.reason_code }}</small>
            </template>
          </el-table-column>
          <el-table-column label="普通任务" min-width="250">
            <template #default="{ row }">
              <div v-if="row.task_id" class="review-identity">
                <code>{{ row.task_id }}</code>
                <el-tag size="small" :type="historyTaskStatusType(row.task_status)">
                  {{ row.task_status }}
                </el-tag>
                <small v-if="row.task_error_code">{{ row.task_error_code }}</small>
              </div>
              <span v-else class="muted">未创建任务</span>
            </template>
          </el-table-column>
          <el-table-column label="证据 / 操作" min-width="190">
            <template #default="{ row }">
              <div class="row-actions">
                <el-tag v-if="row.has_preflight" type="success" size="small">有预演</el-tag>
                <el-button
                  v-if="row.has_preflight"
                  link
                  type="primary"
                  @click="emit('navigate', '预演与确认')"
                  >审核</el-button
                >
                <el-button
                  v-else-if="row.task_id"
                  link
                  type="primary"
                  @click="emit('navigate', '任务中心')"
                  >任务中心</el-button
                >
              </div>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </article>
  </div>
  <div v-else-if="page === '清理与对账'">
    <div class="settings-layout">
      <section class="panel">
        <h3>清理 / 对账报告</h3>
        <div class="health-list">
          <div>
            <Server :size="18" />操作记录
            <el-tag type="info">{{ maintenanceReport?.summary.total_journals ?? 0 }} 项</el-tag>
          </div>
          <div>
            <AlertTriangle :size="18" />需要关注
            <el-tag type="danger"
              >{{ maintenanceReport?.summary.attention_required ?? 0 }} 项</el-tag
            >
          </div>
          <div>
            <RefreshCw :size="18" />可只读对账
            <el-tag type="warning"
              >{{ maintenanceReport?.summary.reconcile_supported ?? 0 }} 项</el-tag
            >
          </div>
          <div>
            <ShieldCheck :size="18" />仅人工检查
            <el-tag>{{ maintenanceReport?.summary.manual_only ?? 0 }} 项</el-tag>
          </div>
        </div>
        <el-button type="primary" :loading="maintenanceLoading" @click="refreshMaintenanceReport">
          <RefreshCw :size="15" />刷新报告
        </el-button>
      </section>
      <section class="panel">
        <h3>保留期清理候选</h3>
        <div class="cleanup-value">
          {{ maintenanceReport?.summary.retention_candidates ?? 0 }} <span>个清理候选</span>
        </div>
        <div class="filters section-space">
          <el-input-number v-model="retentionDays" :min="1" :max="3650" :step="1" />
          <span class="muted">天保留期</span>
          <el-button :loading="retentionLoading" @click="refreshRetentionPlan">
            <RefreshCw :size="15" />重新生成安全预览
          </el-button>
        </div>
        <div class="health-list section-space">
          <div>
            <Server :size="18" />已检查
            <el-tag type="info">{{ retentionPlan?.summary.inspected ?? 0 }} 项</el-tag>
          </div>
          <div>
            <Check :size="18" />可安全清理
            <el-tag type="success">{{ retentionPlan?.summary.eligible ?? 0 }} 项</el-tag>
          </div>
          <div>
            <ShieldCheck :size="18" />安全阻断
            <el-tag type="warning">{{ retentionPlan?.summary.blocked ?? 0 }} 项</el-tag>
          </div>
        </div>
        <el-alert
          title="清理只移除 PackBreaker 操作记录，不会删除媒体文件或下载器任务。"
          type="info"
          :closable="false"
          show-icon
        />
      </section>
    </div>

    <el-alert
      v-if="maintenanceReport?.summary.truncated"
      class="section-space"
      title="报告结果已截断"
      description="当前只展示最早更新的 100 条修复项和 100 条保留期候选；汇总计数仍为全量。"
      type="warning"
      :closable="false"
      show-icon
    />

    <el-alert
      v-if="retentionPlan?.summary.truncated"
      class="section-space"
      title="保留期安全预览已截断"
      description="当前仅检查最早更新的 100 条候选。"
      type="warning"
      :closable="false"
      show-icon
    />

    <el-alert
      v-if="retentionResultUnknown && pendingRetentionPurge"
      class="section-space"
      title="上一次清理结果未知"
      type="warning"
      :closable="false"
      show-icon
    >
      <template #default>
        <p>
          任务 {{ pendingRetentionPurge.taskId }} · 记录
          {{ pendingRetentionPurge.journalId }}。请使用下方按钮确认原请求结果，避免重复清理。
        </p>
        <el-button
          type="warning"
          :loading="purgingJournalId === pendingRetentionPurge.journalId"
          @click="executePendingRetentionPurge"
        >
          重试确认同一清理请求
        </el-button>
      </template>
    </el-alert>

    <section class="panel section-space">
      <h3>保留期安全预览</h3>
      <el-empty
        v-if="!retentionLoading && !retentionPlan?.items.length"
        description="当前没有可清理的操作记录"
      />
      <div v-for="item in retentionPlan?.items ?? []" :key="item.journal_id" class="resource-row">
        <span class="file-icon" :class="{ success: item.eligible, warning: !item.eligible }">
          <Check v-if="item.eligible" :size="20" />
          <ShieldCheck v-else :size="20" />
        </span>
        <div>
          <b>{{ item.kind }} · {{ item.status }}</b>
          <p>{{ retentionReasonLabel(item.reason_code) }}</p>
          <small class="muted">任务 {{ item.task_id }} · 记录 {{ item.journal_id }}</small>
        </div>
        <el-button
          v-if="item.eligible"
          type="danger"
          plain
          :disabled="Boolean(purgingJournalId) || retentionResultUnknown"
          :loading="purgingJournalId === item.journal_id"
          @click="purgeRetentionItem(item)"
        >
          清理 payload
        </el-button>
        <el-tag v-else type="warning">{{ retentionReasonLabel(item.reason_code) }}</el-tag>
      </div>
    </section>

    <section class="panel section-space">
      <h3>人工修复清单</h3>
      <el-empty
        v-if="!maintenanceLoading && !maintenanceReport?.repair_items.length"
        description="当前没有需要人工处理的操作记录"
      />
      <div
        v-for="item in maintenanceReport?.repair_items ?? []"
        :key="item.journal_id"
        class="resource-row"
      >
        <span class="file-icon" :class="{ warning: item.manual_required }">
          <AlertTriangle v-if="item.manual_required" :size="20" />
          <RefreshCw v-else :size="20" />
        </span>
        <div>
          <b>{{ item.kind }} · {{ item.status }}</b>
          <p>{{ item.reason }}</p>
          <small class="muted">任务 {{ item.task_id }} · 记录 {{ item.journal_id }}</small>
          <p>
            <small>{{ item.recommended_action }}</small>
          </p>
        </div>
        <el-tag :type="item.action === 'RECONCILE' ? 'warning' : 'danger'">
          {{ item.action === 'RECONCILE' ? '到任务详情对账' : '必须人工检查' }}
        </el-tag>
      </div>
    </section>

    <section class="panel section-space">
      <h3>维护报告候选概览</h3>
      <el-empty
        v-if="!maintenanceLoading && !maintenanceReport?.cleanup_candidates.length"
        description="当前没有清理候选"
      />
      <div
        v-for="item in maintenanceReport?.cleanup_candidates ?? []"
        :key="item.journal_id"
        class="resource-row"
      >
        <span class="file-icon success"><Check :size="20" /></span>
        <div>
          <b>{{ item.kind }} · {{ item.status }}</b>
          <p>{{ item.reason }}</p>
          <small class="muted">任务 {{ item.task_id }} · 记录 {{ item.journal_id }}</small>
          <p>
            <small>{{ item.recommendation }}</small>
          </p>
        </div>
        <el-tag type="info">候选概览</el-tag>
      </div>
    </section>
  </div>
  <div v-else-if="page === '系统设置'" class="panel">
    <el-tabs v-model="settingTab"
      ><el-tab-pane v-for="s in ['通知', '安全与集成', '备份恢复']" :key="s" :name="s" :label="s"
    /></el-tabs>
    <div v-if="settingTab === '通知'" class="settings-content">
      <NotificationManagement />
    </div>
    <div v-else-if="settingTab === '安全与集成'" class="settings-content">
      <h3 class="detail-section-title">管理员与访问</h3>
      <div class="setting-row">
        <div>
          <b>管理员会话</b>
        </div>
        <el-tag type="success">已认证</el-tag>
      </div>
      <ApiTokenManagement />
    </div>
    <div v-else class="settings-content">
      <h3>备份与恢复</h3>
      <BackupManagement />
    </div>
  </div>
  <UpgradeCenter v-else-if="page === '升级中心'" />
  <el-dialog v-model="scanDialog" title="新建历史扫描" width="min(560px, 94vw)"
    ><el-form label-position="top"
      ><el-form-item label="扫描根目录"><el-input v-model="scanPath" /></el-form-item
      ><el-form-item label="识别类型"
        ><el-radio-group v-model="scanKind"
          ><el-radio value="影片" /><el-radio value="剧集" /></el-radio-group></el-form-item
      ><el-form-item label="文件类型"><el-input v-model="scanTypes" /></el-form-item
      ><el-form-item label="排除规则"><el-input v-model="scanExclude" /></el-form-item></el-form
    ><template #footer
      ><el-button @click="scanDialog = false">取消</el-button
      ><el-button type="primary" :loading="scanLoading" @click="startScan"
        >创建并开始扫描</el-button
      ></template
    ></el-dialog
  >
</template>
