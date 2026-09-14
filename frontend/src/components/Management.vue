<script setup lang="ts">
import { computed, onUnmounted, reactive, ref, watch } from 'vue';
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
  LockKeyhole,
  RotateCcw,
  Plus,
} from '@lucide/vue';
import { ApiProblem } from '../api/client';
import type { Task } from '../demo';
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
  export: [unknown, string];
  open: [Task];
  createHistory: [string];
  navigate: [string];
}>();
const rules = reactive({
  automation: false,
  skip: false,
  repair: '人工引导',
  tag: '大包',
  stable: 60,
  interval: 60,
  concurrency: 1,
  exclude: 'sample, trailer, extras',
  extensions: '.mkv, .mp4, .m2ts',
  episodes: '按集拆包',
  approval: '全部人工确认',
  timeout: 20,
  retries: 2,
});
const savedRules = ref('');
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
      TASK_CHECKPOINT_REFERENCE: '任务检查点仍引用',
      ACTION_RECEIPT_REFERENCE: '动作回执仍引用',
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
      '确认 operation journal 保留期清理',
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
      ElMessage.warning('清理响应结果未知；只能使用冻结的同一 Journal 与 Idempotency-Key 重试确认');
    } else {
      pendingRetentionPurge.value = undefined;
      retentionResultUnknown.value = false;
      ElMessage.error(error instanceof Error ? error.message : 'operation journal 清理失败');
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
const settingTab = ref('常规'),
  settings = reactive({
    timezone: 'Asia/Shanghai',
    retention: 30,
    session: 120,
    notification: '任务完成与失败',
    port: 8000,
  });
</script>
<template>
  <OperationalLogs v-if="page === '日志'" />
  <DownloaderManagement v-else-if="page === '下载器'" />
  <SiteManagement v-else-if="page === '站点管理'" />
  <div v-else-if="page === '规则配置'" class="settings-layout">
    <div class="panel">
      <h3>触发与处理规则</h3>
      <el-form label-position="top"
        ><div class="form-grid">
          <el-form-item label="命中的分类 / 标签"><el-input v-model="rules.tag" /></el-form-item
          ><el-form-item label="源任务稳定期（秒）"
            ><el-input-number v-model="rules.stable" :min="30" :max="3600" /></el-form-item
          ><el-form-item label="轮询间隔（秒）"
            ><el-input-number v-model="rules.interval" :min="30" :max="3600" /></el-form-item
          ><el-form-item label="重磁盘验证并发"
            ><el-input-number v-model="rules.concurrency" :min="1" :max="4" /></el-form-item
          ><el-form-item label="剧集处理方式"
            ><el-select v-model="rules.episodes"
              ><el-option value="按集拆包" /><el-option
                value="按季拆包" /></el-select></el-form-item
          ><el-form-item label="确认策略"
            ><el-select v-model="rules.approval"
              ><el-option value="全部人工确认" /><el-option
                value="完整验证后按规则自动确认" /></el-select
          ></el-form-item>
        </div>
        <el-form-item label="媒体扩展名"><el-input v-model="rules.extensions" /></el-form-item
        ><el-form-item label="排除目录与关键词"><el-input v-model="rules.exclude" /></el-form-item>
        <div class="setting-row">
          <div>
            <b>启用自动任务触发</b>
            <p>仅影响演示配置，默认关闭</p>
          </div>
          <el-switch v-model="rules.automation" />
        </div>
        <el-button
          type="primary"
          @click="
            savedRules = JSON.stringify(rules);
            ElMessage.success('规则已保存在当前演示会话');
          "
          >保存演示规则</el-button
        ><span v-if="savedRules" class="saved-label">已保存</span></el-form
      >
    </div>
    <div class="panel">
      <h3>安全与修复策略</h3>
      <div class="setting-row">
        <div>
          <b>qB 允许跳过客户端校验</b>
          <p>仅完整 piece 验证通过时生效</p>
        </div>
        <el-switch v-model="rules.skip" />
      </div>
      <el-alert
        v-if="rules.skip"
        title="模拟配置已启用。CLIENT_CHECK_REQUIRED 和 Transmission 仍必须校验。"
        type="warning"
        :closable="false"
      /><el-form label-position="top" class="form-stack"
        ><el-form-item label="99% 修复默认模式"
          ><el-select v-model="rules.repair"
            ><el-option value="人工引导" /><el-option value="仅文件级修复" /><el-option
              value="自动 piece 修复" /></el-select></el-form-item
      ></el-form>
      <div
        class="locked-rule"
        v-for="s in [
          '源数据只读',
          '拒绝路径穿越与不安全路径',
          '清理仅限操作日志登记资源',
          '硬链接修复前必须隔离 inode',
          '抽样 hash 不作为执行依据',
        ]"
        :key="s"
      >
        <LockKeyhole :size="15" />{{ s }}
      </div>
      <p class="muted">自动匹配阈值待真实语料标定，本轮保持人工确认默认值。</p>
    </div>
  </div>
  <div v-else-if="page === '历史辅种'">
    <div class="section-heading">
      <h2>历史目录扫描 <small>增量识别影片与剧集，复用安全预演流程</small></h2>
      <el-button type="primary" @click="scanDialog = true"><Plus :size="15" />新建扫描</el-button>
    </div>
    <div class="stats mini-stats">
      <div class="stat">
        <div>扫描根目录</div>
        <strong>{{ scans.length }}</strong
        ><small>真实 /data 目录，只读元数据扫描</small>
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
        <el-alert
          title="批量 Analyze 仅复用现有只读搜站 / 验证 / preflight 流程；source_root 由服务端扫描证据派生。这里不会自动批准候选，也不会创建硬链接或调用下载器写接口。"
          type="info"
          :closable="false"
        />
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
            ><Search :size="15" />批量 Analyze
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
          <span class="muted"
            >筛选由服务端执行，不受当前 500 条视图限制；Analyze / RETRY 每批最多 10 个任务。</span
          >
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
              <span v-else class="muted">未创建 Task</span>
            </template>
          </el-table-column>
          <el-table-column label="证据 / 操作" min-width="190">
            <template #default="{ row }">
              <div class="row-actions">
                <el-tag v-if="row.has_preflight" type="success" size="small">有 Preflight</el-tag>
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
        <p class="muted">
          只读汇总 operation journal
          的阻断状态和可证明动作；不会删除资源、重放副作用或强制改写状态。
        </p>
        <div class="health-list">
          <div>
            <Server :size="18" />Operation journal
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
        <p class="muted">
          维护报告只给出 NOOP / ROLLED_BACK 候选；真正授权必须由服务端 retention-plan
          重新证明任务终态、保留期与零恢复引用。这里只允许逐条删除 operation payload，不做批量清理。
        </p>
        <div class="cleanup-value">
          {{ maintenanceReport?.summary.retention_candidates ?? 0 }} <span>个 journal 候选</span>
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
          title="清理只移除 operation journal 的敏感 payload"
          description="不会删除媒体文件、下载器任务或其他外部资源。服务端会在写事务中再次验证安全门，并保留最小 tombstone 维持审计与历史幂等键封锁。"
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
      description="当前仅检查最早更新的 100 条候选；未出现在预览中的 journal 不会获得前端清理入口。"
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
          任务 {{ pendingRetentionPurge.taskId }} · Journal
          {{ pendingRetentionPurge.journalId }}。不要生成新请求； 只能使用已冻结的同一
          Idempotency-Key 重试确认服务端最终结果。
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
      <p class="muted">
        这是当前服务端安全事实的只读快照。点击清理后，服务端仍会在写事务中重新证明；预览本身不构成写授权。
      </p>
      <el-empty
        v-if="!retentionLoading && !retentionPlan?.items.length"
        description="当前没有可检查的 NOOP / ROLLED_BACK journal"
      />
      <div v-for="item in retentionPlan?.items ?? []" :key="item.journal_id" class="resource-row">
        <span class="file-icon" :class="{ success: item.eligible, warning: !item.eligible }">
          <Check v-if="item.eligible" :size="20" />
          <ShieldCheck v-else :size="20" />
        </span>
        <div>
          <b>{{ item.kind }} · {{ item.status }}</b>
          <p>{{ retentionReasonLabel(item.reason_code) }}</p>
          <small class="muted">任务 {{ item.task_id }} · Journal {{ item.journal_id }}</small>
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
        description="当前没有 RECONCILE_REQUIRED / ROLLBACK_BLOCKED journal"
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
          <small class="muted">任务 {{ item.task_id }} · Journal {{ item.journal_id }}</small>
          <p>
            <small>{{ item.recommended_action }}</small>
          </p>
        </div>
        <el-tag :type="item.action === 'RECONCILE' ? 'warning' : 'danger'">
          {{ item.action === 'RECONCILE' ? '到任务详情只读对账' : '必须人工检查' }}
        </el-tag>
      </div>
    </section>

    <section class="panel section-space">
      <h3>维护报告候选概览</h3>
      <p class="muted">此列表仅用于说明候选原因；是否可清理由上方实时 retention-plan 决定。</p>
      <el-empty
        v-if="!maintenanceLoading && !maintenanceReport?.cleanup_candidates.length"
        description="当前没有 NOOP / ROLLED_BACK journal 候选"
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
          <small class="muted">任务 {{ item.task_id }} · Journal {{ item.journal_id }}</small>
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
      ><el-tab-pane
        v-for="s in ['常规', '通知', '安全与集成', '备份恢复']"
        :key="s"
        :name="s"
        :label="s"
    /></el-tabs>
    <div class="settings-content" v-if="settingTab === '常规'">
      <h3>常规设置</h3>
      <el-form label-position="top"
        ><div class="form-grid">
          <el-form-item label="显示时区"
            ><el-select v-model="settings.timezone"
              ><el-option value="Asia/Shanghai" /><el-option
                value="UTC" /></el-select></el-form-item
          ><el-form-item label="日志保留（天）"
            ><el-input-number v-model="settings.retention" :min="1" :max="365" /></el-form-item
          ><el-form-item label="会话有效期（分钟）"
            ><el-input-number v-model="settings.session" :min="15" :max="1440"
          /></el-form-item>
        </div>
        <el-button
          type="primary"
          @click="ElMessage.success('演示设置已保存；时间格式将在真实数据接入后应用')"
          >保存演示设置</el-button
        ></el-form
      >
      <h3 class="detail-section-title">健康状态</h3>
      <div class="health-list">
        <div v-for="h in ['进程存活', '数据库可写', '任务 Worker 就绪']" :key="h">
          <Check :size="17" />{{ h }}<el-tag type="success">演示正常</el-tag>
        </div>
      </div>
    </div>
    <div v-else-if="settingTab === '通知'" class="settings-content">
      <NotificationManagement />
    </div>
    <div v-else-if="settingTab === '安全与集成'" class="settings-content">
      <el-alert
        title="管理员会话、CSRF、API Token 与加密 secret store 已接入真实后端。API Token 明文只在创建时展示一次。"
        type="success"
        :closable="false"
      />
      <h3 class="detail-section-title">管理员与访问</h3>
      <div class="setting-row">
        <div>
          <b>单管理员安全会话</b>
          <p>Argon2id 强哈希 · 持久会话撤销 · CSRF · 登录双维度限速</p>
        </div>
        <el-tag type="success">已认证</el-tag>
      </div>
      <ApiTokenManagement />
      <h3 class="detail-section-title">下载完成 Webhook</h3>
      <code class="code-block">POST /api/v1/integrations/download-completed</code>
      <div class="check-grid">
        <div
          v-for="s in [
            'HMAC-SHA256 签名',
            '时间戳窗口 300 秒',
            'Nonce 防重放',
            'Idempotency-Key 去重',
          ]"
          :key="s"
        >
          <ShieldCheck :size="15" />{{ s }}
        </div>
      </div>
      <p class="muted">上方为计划契约，本原型未开放真实接口。</p>
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
      ><el-form-item label="排除规则"><el-input v-model="scanExclude" /></el-form-item
      ><el-alert
        title="真实只读增量扫描：仅记录 /data 内普通文件元数据；完成后可将当前快照幂等转换为 PENDING 任务，不会自动搜站、执行硬链接或写入下载器。"
        type="info"
        :closable="false" /></el-form
    ><template #footer
      ><el-button @click="scanDialog = false">取消</el-button
      ><el-button type="primary" :loading="scanLoading" @click="startScan"
        >创建并开始扫描</el-button
      ></template
    ></el-dialog
  >
</template>
