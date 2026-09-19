<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Clock3, FolderSearch, Plus, RefreshCw, RotateCcw } from '@lucide/vue';

import { toApiProblem } from '../api/client';
import {
  listDownloaders,
  listDownloaderTorrents,
  type Downloader,
  type DownloaderTorrent,
} from '../api/downloaders';
import { listSites, type Site } from '../api/sites';
import {
  advanceTaskDefinitionExecution,
  browseTaskDirectories,
  createTaskDefinition,
  decideTaskDefinitionExecutionApproval,
  deleteTaskDefinition,
  executeManualTaskDefinition,
  getTaskDefinitionExecution,
  listTaskDefinitionExecutions,
  listTaskDefinitions,
  precheckTaskDefinition,
  previewTaskCron,
  previewTaskDirectory,
  retryFailedTaskDefinitionExecution,
  scanMonitorTaskDefinition,
  setTaskDefinitionPaused,
  updateTaskDefinition,
  type TaskConflictPolicy,
  type TaskCronPreview,
  type TaskDefinition,
  type TaskDefinitionCreateInput,
  type TaskDefinitionKind,
  type TaskDirectoryEntry,
  type TaskDirectoryFile,
  type TaskDirectoryPreview,
  type TaskExecution,
  type TaskExecutionListItem,
  type TaskFilterInput,
  type TaskInitialScope,
  type TaskOverlapPolicy,
  type TaskSourceKind,
  type TaskStorageMode,
} from '../api/taskDefinitions';
import { taskEventTranslation } from '../taskExecutionEvents';
import { taskLifecycleAdvanceFeedback } from '../taskLifecycleFeedback';
import TaskExecutionEvidencePanel from './TaskExecutionEvidencePanel.vue';

const emit = defineEmits<{ navigate: [page: string] }>();
const LOG_CONTEXT_STORAGE_KEY = 'packbreaker.log-context.v1';

const VIDEO_EXTENSIONS = ['.mkv', '.mp4', '.ts', '.m2ts', '.avi', '.mov', '.wmv'];
const ARCHIVE_EXTENSIONS = ['.rar', '.zip', '.7z', '.tar', '.gz'];
const TEMP_PATTERNS = ['*.part', '*.tmp', '*.crdownload', '*.!qB', '*.aria2'];
type CronInputMode = 'VISUAL' | 'CRON';
type CronVisualKind = 'EVERY_MINUTES' | 'EVERY_HOURS' | 'DAILY' | 'WEEKLY' | 'MONTHLY';

interface Draft {
  name: string;
  kind: TaskDefinitionKind;
  siteId: string;
  sourceKind: TaskSourceKind;
  downloaderId: string;
  selectedTorrentHashes: string[];
  directoryPath: string;
  targetDownloaderId: string;
  cronExpression: string;
  fileTypes: string[];
  minSizeMb: number | null;
  maxSizeMb: number | null;
  includeName: string;
  excludeNames: string;
  ignoreTempFiles: boolean;
  includeSubdirectories: boolean;
  maxScanDepth: number | null;
  outputDirectory: string;
  storageMode: TaskStorageMode;
  preserveStructure: boolean;
  conflictPolicy: TaskConflictPolicy;
  stabilityDetectionEnabled: boolean;
  stabilityWaitSeconds: number;
  onlyCompletedDownloads: boolean;
  initialScope: TaskInitialScope;
  debounceSeconds: number;
  overlapPolicy: TaskOverlapPolicy;
  autoRetryEnabled: boolean;
  maxAutoRetries: number;
  highRiskPreauthorizationEnabled: boolean;
  highRiskAllowedActionKinds: string;
}

const definitions = ref<TaskDefinition[]>([]);
const sites = ref<Site[]>([]);
const downloaders = ref<Downloader[]>([]);
const activeKind = ref<TaskDefinitionKind>('MANUAL');
const loading = ref(false);
const saving = ref(false);
const editingDefinitionId = ref<string | null>(null);
const hydratingDraft = ref(false);
const executing = ref<Record<string, boolean>>({});
const scanning = ref<Record<string, boolean>>({});
const retrying = ref<Record<string, boolean>>({});
const advancingExecution = ref(false);
const decidingApproval = ref<Record<string, boolean>>({});
const togglingPause = ref<Record<string, boolean>>({});
const dialogVisible = ref(false);
const definitionDrawerVisible = ref(false);
const detailDefinition = ref<TaskDefinition | null>(null);
const detailTab = ref('overview');
const executionHistoryLoading = ref(false);
const executionHistory = ref<TaskExecutionListItem[]>([]);
const executionHistoryTotal = ref(0);
const executionHistoryPage = ref(1);
const executionHistoryPageSize = ref(20);
const executionStatusFilter = ref('');
const executionTriggerFilter = ref('');
const executionSearch = ref('');
const executionTimeRange = ref<[Date, Date] | null>(null);
const executionDrawerVisible = ref(false);
const activeExecution = ref<TaskExecution | null>(null);
const executionEvidenceTaskId = ref('');
const torrentDrawerVisible = ref(false);
const torrentLoading = ref(false);
const torrentItems = ref<DownloaderTorrent[]>([]);
const torrentTotal = ref(0);
const torrentPage = ref(1);
const torrentPageSize = ref(50);
const torrentSearch = ref('');
const torrentStatus = ref('');
const torrentCategory = ref('');
const torrentTag = ref('');
const torrentTracker = ref('');
const torrentSavePath = ref('');
const selectedTorrentSnapshots = ref<Record<string, DownloaderTorrent>>({});
const directoryDrawerVisible = ref(false);
const directoryBrowserLoading = ref(false);
const directoryBrowserPath = ref('.');
const directoryEntries = ref<TaskDirectoryEntry[]>([]);
const directoryTarget = ref<'source' | 'output'>('source');
const directoryPreviewLoading = ref(false);
const directoryPreviewVisible = ref(false);
const directoryPreview = ref<TaskDirectoryPreview | null>(null);
const selectedDirectoryPaths = ref<string[]>([]);
const cronInputMode = ref<CronInputMode>('CRON');
const cronVisualKind = ref<CronVisualKind>('EVERY_HOURS');
const cronVisualInterval = ref(2);
const cronVisualHour = ref(0);
const cronVisualMinute = ref(0);
const cronVisualWeekday = ref(1);
const cronVisualMonthDay = ref(1);
const cronPreviewLoading = ref(false);
const cronPreview = ref<TaskCronPreview | null>(null);
const cronPreviewError = ref('');
let cronPreviewTimer: ReturnType<typeof setTimeout> | null = null;
let cronPreviewSequence = 0;

const draft = reactive<Draft>(freshDraft());

const executionEvidenceItems = computed(() =>
  (activeExecution.value?.items ?? []).flatMap((item) =>
    item.unpack_task_id
      ? [{ itemId: item.id, taskId: item.unpack_task_id, name: item.name, closure: item.closure }]
      : [],
  ),
);

watch(
  executionEvidenceItems,
  (items) => {
    if (!items.some((item) => item.taskId === executionEvidenceTaskId.value)) {
      executionEvidenceTaskId.value = items[0]?.taskId ?? '';
    }
  },
  { immediate: true },
);

const manualCount = computed(
  () => definitions.value.filter((item) => item.kind === 'MANUAL').length,
);
const monitorCount = computed(
  () => definitions.value.filter((item) => item.kind === 'MONITOR').length,
);
const manualStats = computed(() => {
  const items = definitions.value.filter((item) => item.kind === 'MANUAL');
  return {
    running: items.filter((item) => item.latest_execution?.status === 'RUNNING').length,
    waiting: items.filter((item) => item.latest_execution?.status === 'PENDING').length,
    failed: items.filter((item) => (item.latest_execution?.failed_count ?? 0) > 0).length,
    completedToday: items.filter((item) => isToday(item.latest_execution?.finished_at)).length,
  };
});
const monitorStats = computed(() => {
  const items = definitions.value.filter((item) => item.kind === 'MONITOR');
  return {
    enabled: items.filter((item) => item.status === 'ENABLED').length,
    paused: items.filter((item) => item.status === 'PAUSED').length,
    error: items.filter((item) => ['ERROR', 'SITE_UNAVAILABLE'].includes(item.status)).length,
    triggeredToday: items.filter((item) => isToday(item.latest_execution?.created_at)).length,
  };
});
const visibleDefinitions = computed(() =>
  definitions.value.filter((item) => item.kind === activeKind.value),
);
const configuredSites = computed(() => sites.value);
const configuredDownloaders = computed(() => downloaders.value);
const selectedTorrentCount = computed(() => draft.selectedTorrentHashes.length);
const selectedTorrentSize = computed(() =>
  draft.selectedTorrentHashes.reduce(
    (total, hash) => total + (selectedTorrentSnapshots.value[hash]?.size_bytes ?? 0),
    0,
  ),
);
const selectedDirectoryFiles = computed(() =>
  (directoryPreview.value?.files ?? []).filter((item) =>
    selectedDirectoryPaths.value.includes(item.relative_path),
  ),
);
const selectedDirectorySize = computed(() =>
  selectedDirectoryFiles.value.reduce((total, item) => total + item.size_bytes, 0),
);
const directoryParentPath = computed(() => {
  if (directoryBrowserPath.value === '.') return null;
  const parts = directoryBrowserPath.value.split('/');
  parts.pop();
  return parts.length ? parts.join('/') : '.';
});

onMounted(() => void refresh());

watch(
  () => draft.downloaderId,
  (next, previous) => {
    if (hydratingDraft.value) return;
    if (previous && next !== previous) {
      draft.selectedTorrentHashes = [];
      selectedTorrentSnapshots.value = {};
    }
  },
);

watch(
  () => [
    draft.directoryPath,
    draft.fileTypes.join('|'),
    draft.minSizeMb,
    draft.maxSizeMb,
    draft.includeName,
    draft.excludeNames,
    draft.ignoreTempFiles,
    draft.includeSubdirectories,
    draft.maxScanDepth,
  ],
  () => {
    if (hydratingDraft.value) return;
    directoryPreview.value = null;
    selectedDirectoryPaths.value = [];
  },
);

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
  () => [draft.kind, draft.cronExpression],
  () => scheduleCronPreview(),
);

function applyVisualCron(): void {
  const interval = Math.max(1, Math.trunc(cronVisualInterval.value || 1));
  const hour = Math.min(23, Math.max(0, Math.trunc(cronVisualHour.value || 0)));
  const minute = Math.min(59, Math.max(0, Math.trunc(cronVisualMinute.value || 0)));
  const weekday = Math.min(6, Math.max(0, Math.trunc(cronVisualWeekday.value || 0)));
  const monthDay = Math.min(31, Math.max(1, Math.trunc(cronVisualMonthDay.value || 1)));
  if (cronVisualKind.value === 'EVERY_MINUTES') {
    draft.cronExpression = `*/${Math.min(59, interval)} * * * *`;
  } else if (cronVisualKind.value === 'EVERY_HOURS') {
    draft.cronExpression = `${minute} */${Math.min(23, interval)} * * *`;
  } else if (cronVisualKind.value === 'DAILY') {
    draft.cronExpression = `${minute} ${hour} * * *`;
  } else if (cronVisualKind.value === 'WEEKLY') {
    draft.cronExpression = `${minute} ${hour} * * ${weekday}`;
  } else {
    draft.cronExpression = `${minute} ${hour} ${monthDay} * *`;
  }
}

function scheduleCronPreview(): void {
  if (cronPreviewTimer !== null) clearTimeout(cronPreviewTimer);
  cronPreviewTimer = setTimeout(() => void refreshCronPreview(), 300);
}

async function refreshCronPreview(): Promise<void> {
  const expression = draft.cronExpression.trim();
  const sequence = ++cronPreviewSequence;
  if (draft.kind !== 'MONITOR' || !expression) {
    cronPreview.value = null;
    cronPreviewError.value = '';
    cronPreviewLoading.value = false;
    return;
  }
  cronPreviewLoading.value = true;
  try {
    const result = await previewTaskCron(expression);
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

function freshDraft(): Draft {
  return {
    name: '',
    kind: 'MANUAL',
    siteId: '',
    sourceKind: 'DOWNLOADER',
    downloaderId: '',
    selectedTorrentHashes: [],
    directoryPath: '',
    targetDownloaderId: '',
    cronExpression: '',
    fileTypes: ['VIDEO'],
    minSizeMb: null,
    maxSizeMb: null,
    includeName: '',
    excludeNames: 'sample, trailer',
    ignoreTempFiles: true,
    includeSubdirectories: true,
    maxScanDepth: null,
    outputDirectory: '',
    storageMode: 'HARDLINK',
    preserveStructure: true,
    conflictPolicy: 'VERIFY_REUSE_OR_STOP',
    stabilityDetectionEnabled: true,
    stabilityWaitSeconds: 60,
    onlyCompletedDownloads: true,
    initialScope: 'NEW_ONLY',
    debounceSeconds: 30,
    overlapPolicy: 'SKIP',
    autoRetryEnabled: true,
    maxAutoRetries: 3,
    highRiskPreauthorizationEnabled: false,
    highRiskAllowedActionKinds: '',
  };
}

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    const [nextDefinitions, nextSites, nextDownloaders] = await Promise.all([
      listTaskDefinitions(),
      listSites(),
      listDownloaders(),
    ]);
    definitions.value = nextDefinitions;
    if (detailDefinition.value) {
      detailDefinition.value =
        nextDefinitions.find((item) => item.id === detailDefinition.value?.id) ??
        detailDefinition.value;
    }
    sites.value = nextSites;
    downloaders.value = nextDownloaders;
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    loading.value = false;
  }
}

function openCreate(kind: TaskDefinitionKind = activeKind.value): void {
  editingDefinitionId.value = null;
  Object.assign(draft, freshDraft(), { kind });
  cronInputMode.value = 'CRON';
  cronPreview.value = null;
  cronPreviewError.value = '';
  selectedTorrentSnapshots.value = {};
  directoryPreview.value = null;
  selectedDirectoryPaths.value = [];
  dialogVisible.value = true;
}

function openEdit(item: TaskDefinition): void {
  editingDefinitionId.value = item.id;
  hydrateDraft(item, false);
  dialogVisible.value = true;
}

function openClone(item: TaskDefinition): void {
  editingDefinitionId.value = null;
  hydrateDraft(item, true);
  dialogVisible.value = true;
}

function hydrateDraft(item: TaskDefinition, clone: boolean): void {
  hydratingDraft.value = true;
  cronInputMode.value = 'CRON';
  cronPreview.value = null;
  cronPreviewError.value = '';
  const selectedTorrents = Array.isArray(item.source_config.selected_torrents)
    ? item.source_config.selected_torrents
    : [];
  const torrentSnapshots: Record<string, DownloaderTorrent> = {};
  for (const raw of selectedTorrents) {
    if (!raw || typeof raw !== 'object') continue;
    const value = raw as Partial<DownloaderTorrent>;
    if (!value.torrent_hash || !value.name) continue;
    torrentSnapshots[value.torrent_hash] = {
      torrent_hash: value.torrent_hash,
      name: value.name,
      status: value.status ?? '',
      progress: value.progress ?? 0,
      size_bytes: value.size_bytes ?? 0,
      category: value.category ?? null,
      tags: value.tags ?? [],
      tracker: value.tracker ?? null,
      save_path: value.save_path ?? '',
      content_path: value.content_path ?? null,
    };
  }
  selectedTorrentSnapshots.value = torrentSnapshots;

  const selectedFiles = Array.isArray(item.source_config.selected_files)
    ? item.source_config.selected_files
    : [];
  const directoryFiles: TaskDirectoryFile[] = selectedFiles.flatMap((raw) => {
    if (!raw || typeof raw !== 'object') return [];
    const value = raw as Partial<TaskDirectoryFile>;
    if (
      typeof value.relative_path !== 'string' ||
      typeof value.size_bytes !== 'number' ||
      typeof value.device !== 'number' ||
      typeof value.inode !== 'number' ||
      typeof value.mtime_ns !== 'number'
    ) {
      return [];
    }
    return [value as TaskDirectoryFile];
  });
  directoryPreview.value = directoryFiles.length
    ? {
        directory_path: item.source_directory ?? '.',
        matched_count: directoryFiles.length,
        total_size_bytes: directoryFiles.reduce((total, file) => total + file.size_bytes, 0),
        files: directoryFiles,
      }
    : null;
  selectedDirectoryPaths.value = directoryFiles.map((file) => file.relative_path);

  Object.assign(draft, {
    name: clone ? '' : item.name,
    kind: item.kind,
    siteId: item.site_id ?? '',
    sourceKind: item.source_kind,
    downloaderId: item.source_downloader_id ?? '',
    selectedTorrentHashes: Object.keys(torrentSnapshots),
    directoryPath: item.source_directory ?? '',
    targetDownloaderId:
      typeof item.source_config.target_downloader_id === 'string'
        ? item.source_config.target_downloader_id
        : '',
    cronExpression: item.cron_expression ?? '',
    fileTypes: [...item.file_types],
    minSizeMb: item.min_size_bytes === null ? null : item.min_size_bytes / 1024 / 1024,
    maxSizeMb: item.max_size_bytes === null ? null : item.max_size_bytes / 1024 / 1024,
    includeName: item.include_name ?? '',
    excludeNames: item.exclude_names.join(', '),
    ignoreTempFiles: item.ignore_temp_files,
    includeSubdirectories: item.include_subdirectories,
    maxScanDepth: item.max_scan_depth,
    outputDirectory: item.output_directory,
    storageMode: item.storage_mode,
    preserveStructure: item.preserve_structure,
    conflictPolicy: item.conflict_policy,
    stabilityDetectionEnabled: item.stability_detection_enabled,
    stabilityWaitSeconds: item.stability_wait_seconds,
    onlyCompletedDownloads: item.only_completed_downloads,
    initialScope: item.initial_scope,
    debounceSeconds: item.debounce_seconds,
    overlapPolicy: item.overlap_policy,
    autoRetryEnabled: item.auto_retry_enabled,
    maxAutoRetries: item.max_auto_retries,
    highRiskPreauthorizationEnabled: item.high_risk_preauthorization_enabled,
    highRiskAllowedActionKinds: item.high_risk_allowed_action_kinds.join(', '),
  });
  void nextTick(() => {
    hydratingDraft.value = false;
  });
}

async function removeDefinition(item: TaskDefinition): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `删除任务“${item.name}”后将停止新的调度；历史执行记录会保留。`,
      '删除任务',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    );
    await deleteTaskDefinition(item.id);
    definitions.value = definitions.value.filter((value) => value.id !== item.id);
    if (detailDefinition.value?.id === item.id) definitionDrawerVisible.value = false;
    ElMessage.success('任务已删除，历史执行记录已保留');
  } catch (caught) {
    if (caught === 'cancel' || caught === 'close') return;
    ElMessage.error(toApiProblem(caught).message);
  }
}

function filterPayload(): TaskFilterInput {
  const excludeNames = draft.excludeNames
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean);
  return {
    file_types: draft.fileTypes,
    video_extensions: VIDEO_EXTENSIONS,
    archive_extensions: ARCHIVE_EXTENSIONS,
    min_size_bytes: toBytes(draft.minSizeMb),
    max_size_bytes: toBytes(draft.maxSizeMb),
    include_name: draft.includeName.trim() || null,
    exclude_names: excludeNames,
    ignore_temp_files: draft.ignoreTempFiles,
    temp_patterns: TEMP_PATTERNS,
    include_subdirectories: draft.includeSubdirectories,
    max_scan_depth: draft.maxScanDepth,
  };
}

async function openDirectoryDrawer(target: 'source' | 'output'): Promise<void> {
  directoryTarget.value = target;
  const current = target === 'source' ? draft.directoryPath : draft.outputDirectory;
  directoryBrowserPath.value = current.trim() || '.';
  directoryDrawerVisible.value = true;
  await loadDirectoryEntries();
}

async function loadDirectoryEntries(path = directoryBrowserPath.value): Promise<void> {
  directoryBrowserLoading.value = true;
  try {
    const result = await browseTaskDirectories(path);
    directoryBrowserPath.value = path;
    directoryEntries.value = result.entries;
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    directoryBrowserLoading.value = false;
  }
}

function chooseCurrentDirectory(): void {
  if (directoryTarget.value === 'output' && directoryBrowserPath.value === '.') {
    ElMessage.warning('输出目录不能直接使用 /data 根目录，请选择或填写子目录');
    return;
  }
  if (directoryTarget.value === 'source') draft.directoryPath = directoryBrowserPath.value;
  else draft.outputDirectory = directoryBrowserPath.value;
  directoryDrawerVisible.value = false;
}

async function scanDirectoryPreview(): Promise<void> {
  if (!draft.directoryPath.trim()) {
    ElMessage.warning('请先选择来源目录');
    return;
  }
  directoryPreviewLoading.value = true;
  try {
    const result = await previewTaskDirectory(draft.directoryPath.trim(), filterPayload());
    directoryPreview.value = result;
    selectedDirectoryPaths.value = result.files.map((item) => item.relative_path);
    directoryPreviewVisible.value = true;
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    directoryPreviewLoading.value = false;
  }
}

function directoryFileSelected(item: TaskDirectoryFile): boolean {
  return selectedDirectoryPaths.value.includes(item.relative_path);
}

function toggleDirectoryFile(item: TaskDirectoryFile, selected: boolean): void {
  if (selected && !directoryFileSelected(item)) {
    selectedDirectoryPaths.value = [...selectedDirectoryPaths.value, item.relative_path];
  } else if (!selected) {
    selectedDirectoryPaths.value = selectedDirectoryPaths.value.filter(
      (value) => value !== item.relative_path,
    );
  }
}

async function openTorrentDrawer(): Promise<void> {
  if (!draft.downloaderId) {
    ElMessage.warning('请先选择来源下载器');
    return;
  }
  torrentDrawerVisible.value = true;
  torrentPage.value = 1;
  await loadTorrents();
}

async function loadTorrents(): Promise<void> {
  if (!draft.downloaderId) return;
  torrentLoading.value = true;
  try {
    const result = await listDownloaderTorrents(draft.downloaderId, {
      page: torrentPage.value,
      page_size: torrentPageSize.value,
      ...(torrentSearch.value.trim() ? { search: torrentSearch.value.trim() } : {}),
      ...(torrentStatus.value.trim() ? { status: torrentStatus.value.trim() } : {}),
      ...(torrentCategory.value.trim() ? { category: torrentCategory.value.trim() } : {}),
      ...(torrentTag.value.trim() ? { tag: torrentTag.value.trim() } : {}),
      ...(torrentTracker.value.trim() ? { tracker: torrentTracker.value.trim() } : {}),
      ...(torrentSavePath.value.trim() ? { save_path: torrentSavePath.value.trim() } : {}),
    });
    torrentItems.value = result.items;
    torrentTotal.value = result.total;
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    torrentLoading.value = false;
  }
}

function torrentSelected(item: DownloaderTorrent): boolean {
  return draft.selectedTorrentHashes.includes(item.torrent_hash);
}

function toggleTorrent(item: DownloaderTorrent, selected: boolean): void {
  if (selected) {
    if (!draft.selectedTorrentHashes.includes(item.torrent_hash)) {
      draft.selectedTorrentHashes = [...draft.selectedTorrentHashes, item.torrent_hash];
    }
    selectedTorrentSnapshots.value = {
      ...selectedTorrentSnapshots.value,
      [item.torrent_hash]: item,
    };
    return;
  }
  draft.selectedTorrentHashes = draft.selectedTorrentHashes.filter(
    (value) => value !== item.torrent_hash,
  );
  const next = { ...selectedTorrentSnapshots.value };
  delete next[item.torrent_hash];
  selectedTorrentSnapshots.value = next;
}

function formatBytes(value: number): string {
  if (value >= 1024 ** 4) return `${(value / 1024 ** 4).toFixed(2)} TB`;
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.max(0, value)} B`;
}

function siteLabel(site: Site): string {
  const state = site.enabled ? (site.connection_status === 'OK' ? '可用' : '未完成验证') : '已停用';
  return `${site.name} · ${state}`;
}

function kindLabel(kind: TaskDefinitionKind): string {
  return kind === 'MANUAL' ? '手动拆包' : '监控拆包';
}

function sourceText(item: TaskDefinition): string {
  if (item.source_kind === 'DOWNLOADER') return item.source_downloader_name ?? '下载器已删除';
  return item.source_directory ?? '目录未配置';
}

function storageLabel(value: TaskStorageMode): string {
  if (value === 'HARDLINK') return '硬链接';
  if (value === 'SYMLINK') return '软链接';
  return '复制';
}

function statusLabel(item: TaskDefinition): string {
  if (item.status === 'SITE_UNAVAILABLE') return '站点不可用';
  if (item.status === 'PAUSED') return '已暂停';
  if (item.status === 'ERROR') return '异常';
  return item.kind === 'MONITOR' ? '等待调度' : '等待执行';
}

function statusTag(item: TaskDefinition): 'success' | 'warning' | 'danger' | 'info' {
  if (item.status === 'SITE_UNAVAILABLE') return 'warning';
  if (item.status === 'ERROR') return 'danger';
  if (item.status === 'PAUSED') return 'info';
  return 'success';
}

function resultText(item: TaskDefinition): string {
  const latest = item.latest_execution;
  if (!latest) return '暂无执行';
  const skipped = latest.skipped_count ? ` · 跳过 ${latest.skipped_count}` : '';
  return `成功 ${latest.success_count} · 失败 ${latest.failed_count}${skipped}`;
}

function phaseText(item: TaskDefinition): string {
  const phase = item.latest_execution?.phase;
  if (!phase) return item.kind === 'MONITOR' ? '等待调度' : '尚未执行';
  const labels: Record<string, string> = {
    WAITING: '等待执行',
    DISCOVERING: '发现源数据',
    ANALYZING: '分析文件',
    SCANNING_SITE: '扫描站点',
    PREPARING: '准备执行',
    UNPACKING: '正在拆包',
    OUTPUTTING: '写入输出',
    VERIFYING: '验证结果',
    COMPLETED: '已完成',
    FAILED: '失败',
    SKIPPED: '已跳过',
  };
  return labels[phase] ?? phase;
}

function recentActivityTime(item: TaskDefinition): string | null {
  if (item.kind === 'MONITOR') return item.last_scan_at;
  return item.latest_execution?.started_at ?? item.latest_execution?.created_at ?? null;
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value));
}

function isToday(value: string | null | undefined): boolean {
  if (!value) return false;
  const target = new Date(value);
  const now = new Date();
  return (
    target.getFullYear() === now.getFullYear() &&
    target.getMonth() === now.getMonth() &&
    target.getDate() === now.getDate()
  );
}

function formatOptionalTime(value: string | null): string {
  return value ? formatTime(value) : '—';
}

function executionDuration(item: TaskExecutionListItem | TaskExecution): string {
  if (!item.started_at) return '—';
  const end = item.finished_at ? new Date(item.finished_at).getTime() : Date.now();
  const seconds = Math.max(0, Math.round((end - new Date(item.started_at).getTime()) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
}

function triggerLabel(value: string): string {
  if (value === 'MANUAL') return '手动执行';
  if (value === 'CRON') return 'Cron';
  if (value === 'IMMEDIATE_SCAN') return '立即扫描';
  if (value === 'FAILED_RETRY') return '失败重试';
  if (value === 'SYSTEM_RECOVERY') return '系统恢复';
  return value;
}

function prettyJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

async function openDefinitionDetail(item: TaskDefinition): Promise<void> {
  detailDefinition.value = item;
  detailTab.value = 'overview';
  executionHistoryPage.value = 1;
  executionStatusFilter.value = '';
  executionTriggerFilter.value = '';
  executionSearch.value = '';
  executionTimeRange.value = null;
  definitionDrawerVisible.value = true;
  await loadExecutionHistory();
}

async function loadExecutionHistory(): Promise<void> {
  if (!detailDefinition.value) return;
  executionHistoryLoading.value = true;
  try {
    const result = await listTaskDefinitionExecutions(detailDefinition.value.id, {
      page: executionHistoryPage.value,
      page_size: executionHistoryPageSize.value,
      ...(executionStatusFilter.value ? { status: executionStatusFilter.value } : {}),
      ...(executionTriggerFilter.value ? { trigger: executionTriggerFilter.value } : {}),
      ...(executionSearch.value.trim() ? { search: executionSearch.value.trim() } : {}),
      ...(executionTimeRange.value
        ? {
            started_from: executionTimeRange.value[0].toISOString(),
            started_to: executionTimeRange.value[1].toISOString(),
          }
        : {}),
    });
    executionHistory.value = result.items;
    executionHistoryTotal.value = result.total;
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    executionHistoryLoading.value = false;
  }
}

async function openExecutionDetail(executionId: string): Promise<void> {
  if (!detailDefinition.value) return;
  try {
    activeExecution.value = await getTaskDefinitionExecution(
      detailDefinition.value.id,
      executionId,
    );
    executionDrawerVisible.value = true;
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  }
}

async function retryExecution(execution: TaskExecutionListItem | TaskExecution): Promise<void> {
  if (!detailDefinition.value || execution.failed_count === 0) return;
  try {
    activeExecution.value = await retryFailedTaskDefinitionExecution(
      detailDefinition.value.id,
      execution.id,
      crypto.randomUUID(),
    );
    executionDrawerVisible.value = true;
    await loadExecutionHistory();
    await refresh();
    ElMessage.success('已创建失败对象重试执行');
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  }
}

async function retryActiveExecution(): Promise<void> {
  const execution = activeExecution.value;
  const definitionId = execution?.task_definition_id;
  if (!execution || !definitionId || execution.failed_count === 0) return;
  try {
    activeExecution.value = await retryFailedTaskDefinitionExecution(
      definitionId,
      execution.id,
      crypto.randomUUID(),
    );
    await refresh();
    if (detailDefinition.value?.id === definitionId) await loadExecutionHistory();
    ElMessage.success('已创建失败对象重试执行');
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  }
}

function openExecutionLogs(): void {
  const execution = activeExecution.value;
  if (!execution) return;
  const startedAt = new Date(execution.started_at || execution.created_at).getTime();
  const elapsedMinutes = Number.isFinite(startedAt) ? (Date.now() - startedAt) / 60_000 + 10 : 60;
  sessionStorage.setItem(
    LOG_CONTEXT_STORAGE_KEY,
    JSON.stringify({
      task_id: execution.task_definition_id,
      execution_id: execution.id,
      trace_id: execution.trace_id,
      window_minutes: Math.min(10080, Math.max(15, Math.ceil(elapsedMinutes))),
    }),
  );
  executionDrawerVisible.value = false;
  definitionDrawerVisible.value = false;
  emit('navigate', '日志');
}

function validateDraft(): boolean {
  if (!draft.name.trim()) {
    ElMessage.warning('请输入任务名称，任务名称不会自动生成');
    return false;
  }
  if (!draft.siteId) {
    ElMessage.warning('请选择扫描站点');
    return false;
  }
  if (draft.sourceKind === 'DOWNLOADER' && !draft.downloaderId) {
    ElMessage.warning('请选择来源下载器');
    return false;
  }
  if (
    draft.kind === 'MANUAL' &&
    draft.sourceKind === 'DOWNLOADER' &&
    draft.selectedTorrentHashes.length === 0
  ) {
    ElMessage.warning('手动下载器任务至少选择一个真实种子');
    return false;
  }
  if (draft.sourceKind === 'DIRECTORY' && !draft.directoryPath.trim()) {
    ElMessage.warning('请输入来源目录');
    return false;
  }
  if (draft.sourceKind === 'DIRECTORY' && !draft.targetDownloaderId) {
    ElMessage.warning('目录来源任务必须选择目标下载器');
    return false;
  }
  if (
    draft.kind === 'MANUAL' &&
    draft.sourceKind === 'DIRECTORY' &&
    selectedDirectoryPaths.value.length === 0
  ) {
    ElMessage.warning('手动目录任务必须先扫描预览并至少选择一个文件');
    return false;
  }
  if (draft.kind === 'MONITOR' && !draft.cronExpression.trim()) {
    ElMessage.warning('监控任务必须填写 Cron 表达式');
    return false;
  }
  if (
    draft.kind === 'MONITOR' &&
    draft.highRiskPreauthorizationEnabled &&
    !draft.highRiskAllowedActionKinds.trim()
  ) {
    ElMessage.warning('开启高风险预授权时必须填写允许的 action kind 白名单');
    return false;
  }
  if (!draft.fileTypes.length) {
    ElMessage.warning('至少选择一种文件类型');
    return false;
  }
  if (!draft.outputDirectory.trim()) {
    ElMessage.warning('请输入输出目录');
    return false;
  }
  return true;
}

function toBytes(value: number | null): number | null {
  if (value === null) return null;
  return Math.round(value * 1024 * 1024);
}

function createPayload(): TaskDefinitionCreateInput {
  return {
    name: draft.name.trim(),
    kind: draft.kind,
    site_id: draft.siteId,
    source:
      draft.sourceKind === 'DOWNLOADER'
        ? {
            kind: 'DOWNLOADER',
            downloader_id: draft.downloaderId,
            config: {
              selected_torrent_hashes: draft.selectedTorrentHashes,
              selected_torrents: draft.selectedTorrentHashes
                .map((hash) => selectedTorrentSnapshots.value[hash])
                .filter((item): item is DownloaderTorrent => Boolean(item))
                .map((item) => ({
                  torrent_hash: item.torrent_hash,
                  name: item.name,
                  size_bytes: item.size_bytes,
                  save_path: item.save_path,
                  content_path: item.content_path,
                  status: item.status,
                  progress: item.progress,
                })),
            },
          }
        : {
            kind: 'DIRECTORY',
            directory_path: draft.directoryPath.trim(),
            ...(draft.kind === 'MANUAL'
              ? {
                  config: {
                    target_downloader_id: draft.targetDownloaderId,
                    selected_files: selectedDirectoryFiles.value.map((item) => ({
                      relative_path: item.relative_path,
                      size_bytes: item.size_bytes,
                      device: item.device,
                      inode: item.inode,
                      mtime_ns: item.mtime_ns,
                    })),
                  },
                }
              : { config: { target_downloader_id: draft.targetDownloaderId } }),
          },
    ...(draft.kind === 'MONITOR' ? { cron_expression: draft.cronExpression.trim() } : {}),
    filters: filterPayload(),
    output_policy: {
      output_directory: draft.outputDirectory.trim(),
      storage_mode: draft.storageMode,
      preserve_structure: draft.preserveStructure,
      conflict_policy: draft.conflictPolicy,
    },
    execution_policy: {
      stability_detection_enabled: draft.stabilityDetectionEnabled,
      stability_wait_seconds: draft.stabilityWaitSeconds,
      only_completed_downloads: draft.onlyCompletedDownloads,
      initial_scope: draft.initialScope,
      debounce_seconds: draft.debounceSeconds,
      overlap_policy: draft.overlapPolicy,
      auto_retry_enabled: draft.autoRetryEnabled,
      max_auto_retries: draft.maxAutoRetries,
      retry_intervals_seconds: [60, 300, 900],
      high_risk_preauthorization_enabled:
        draft.kind === 'MONITOR' && draft.highRiskPreauthorizationEnabled,
      high_risk_allowed_action_kinds:
        draft.kind === 'MONITOR' && draft.highRiskPreauthorizationEnabled
          ? Array.from(
              new Set(
                draft.highRiskAllowedActionKinds
                  .split(',')
                  .map((value) => value.trim().toUpperCase())
                  .filter(Boolean),
              ),
            )
          : [],
    },
  };
}

async function saveDefinition(): Promise<void> {
  if (!validateDraft()) return;
  saving.value = true;
  try {
    const payload = createPayload();
    const precheck = await precheckTaskDefinition(payload);
    const blocked = precheck.items.filter((item) => item.status === 'BLOCKED');
    if (blocked.length) {
      await ElMessageBox.alert(
        blocked.map((item) => `【${item.title}】${item.detail}`).join('\n'),
        '任务预检未通过',
        { type: 'error', confirmButtonText: '返回修改' },
      );
      return;
    }
    const warnings = precheck.items.filter((item) => item.status === 'WARNING');
    if (warnings.length) {
      await ElMessageBox.confirm(
        `${warnings.map((item) => `【${item.title}】${item.detail}`).join('\n')}\n\n确认继续保存？`,
        '任务预检存在警告',
        { type: 'warning', confirmButtonText: '确认保存', cancelButtonText: '返回修改' },
      );
    }
    const creating = !editingDefinitionId.value;
    const saved = editingDefinitionId.value
      ? await updateTaskDefinition(editingDefinitionId.value, payload)
      : await createTaskDefinition(payload);
    const existingIndex = definitions.value.findIndex((item) => item.id === saved.id);
    if (existingIndex >= 0) {
      definitions.value = definitions.value.map((item) => (item.id === saved.id ? saved : item));
    } else {
      definitions.value = [saved, ...definitions.value];
    }
    activeKind.value = saved.kind;
    dialogVisible.value = false;
    editingDefinitionId.value = null;
    if (creating && saved.kind === 'MANUAL') {
      const execution = await executeManualTaskDefinition(saved.id);
      activeExecution.value = execution;
      executionDrawerVisible.value = true;
      await refresh();
      const materialized = execution.items.filter((value) => value.unpack_task_id).length;
      ElMessage.success(`任务已保存并开始，已物化 ${materialized} 个安全 Run`);
    } else {
      ElMessage.success(existingIndex >= 0 ? '任务定义已更新' : '监控任务已保存并启用');
    }
  } catch (caught) {
    if (caught === 'cancel' || caught === 'close') return;
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    saving.value = false;
  }
}

async function executeDefinition(item: TaskDefinition): Promise<void> {
  if (item.kind !== 'MANUAL') return;
  executing.value = { ...executing.value, [item.id]: true };
  try {
    const execution = await executeManualTaskDefinition(item.id);
    activeExecution.value = execution;
    executionDrawerVisible.value = true;
    await refresh();
    const materialized = execution.items.filter((value) => value.unpack_task_id).length;
    ElMessage.success(`已物化 ${materialized} 个安全 Run，已进入统一安全生命周期`);
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    const next = { ...executing.value };
    delete next[item.id];
    executing.value = next;
  }
}

async function scanDefinition(item: TaskDefinition): Promise<void> {
  if (item.kind !== 'MONITOR') return;
  scanning.value = { ...scanning.value, [item.id]: true };
  try {
    const result = await scanMonitorTaskDefinition(item.id);
    if (result.execution) {
      activeExecution.value = result.execution;
      executionDrawerVisible.value = true;
    }
    await refresh();
    if (result.outcome === 'BASELINE_ESTABLISHED') {
      ElMessage.success(`首次扫描基线已建立，记录 ${result.discovered_count} 个现有对象`);
    } else if (result.outcome === 'BASELINE_CONTINUING') {
      ElMessage.info(
        `大目录基线扫描进行中，已累计扫描 ${result.discovered_count} 个对象，将自动续扫下一批`,
      );
    } else if (result.outcome === 'SCAN_CONTINUING') {
      ElMessage.info(
        `大目录扫描进行中，已累计扫描 ${result.discovered_count} 个对象，将自动续扫下一批`,
      );
    } else if (result.outcome === 'MATERIALIZED') {
      ElMessage.success(`发现 ${result.new_count} 个新对象，已进入安全执行链`);
    } else if (result.outcome === 'STABILITY_WAIT') {
      ElMessage.info(`发现 ${result.new_count} 个新对象，正在等待文件稳定`);
    } else if (result.outcome === 'DEBOUNCE_WAIT') {
      ElMessage.info(`发现 ${result.new_count} 个新对象，正在等待修改静默窗口结束`);
    } else if (result.outcome === 'OVERLAP_QUEUED') {
      ElMessage.info('已有扫描正在执行，已排队在结束后补扫描一次');
    } else if (result.outcome === 'OVERLAP_SKIPPED') {
      ElMessage.info('已有扫描正在执行，本次按重叠策略跳过');
    } else {
      ElMessage.info('扫描完成，没有发现需要处理的新对象');
    }
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    const next = { ...scanning.value };
    delete next[item.id];
    scanning.value = next;
  }
}

async function toggleMonitorPaused(item: TaskDefinition): Promise<void> {
  if (item.kind !== 'MONITOR') return;
  const paused = item.status !== 'PAUSED';
  togglingPause.value = { ...togglingPause.value, [item.id]: true };
  try {
    const updated = await setTaskDefinitionPaused(item.id, paused);
    definitions.value = definitions.value.map((value) => (value.id === item.id ? updated : value));
    if (detailDefinition.value?.id === item.id) detailDefinition.value = updated;
    ElMessage.success(paused ? '监控任务已暂停' : '监控任务已恢复，将按下一次 Cron 继续扫描');
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    const next = { ...togglingPause.value };
    delete next[item.id];
    togglingPause.value = next;
  }
}

async function retryFailed(item: TaskDefinition): Promise<void> {
  const latest = item.latest_execution;
  if (!latest || latest.failed_count === 0) {
    ElMessage.info('最近一次执行没有可重试的失败对象');
    return;
  }
  retrying.value = { ...retrying.value, [item.id]: true };
  try {
    const execution = await retryFailedTaskDefinitionExecution(
      item.id,
      latest.id,
      crypto.randomUUID(),
    );
    activeExecution.value = execution;
    executionDrawerVisible.value = true;
    if (detailDefinition.value?.id === item.id) await loadExecutionHistory();
    await refresh();
    ElMessage.success(`已创建失败对象重试执行，共 ${execution.discovered_count} 个对象`);
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    const next = { ...retrying.value };
    delete next[item.id];
    retrying.value = next;
  }
}

async function advanceActiveExecution(): Promise<void> {
  const execution = activeExecution.value;
  const definitionId = execution?.task_definition_id;
  if (!execution || !definitionId || advancingExecution.value) return;
  advancingExecution.value = true;
  try {
    const updated = await advanceTaskDefinitionExecution(
      definitionId,
      execution.id,
      `task-lifecycle-${execution.id}-${crypto.randomUUID()}`,
    );
    activeExecution.value = updated;
    if (detailDefinition.value?.id === definitionId) await loadExecutionHistory();
    await refresh();
    const feedback = taskLifecycleAdvanceFeedback(updated.items);
    if (feedback.level === 'info') ElMessage.info(feedback.message);
    else if (feedback.level === 'warning') ElMessage.warning(feedback.message);
    else ElMessage.success(feedback.message);
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    advancingExecution.value = false;
  }
}

async function decideApproval(
  item: TaskExecution['items'][number],
  approve: boolean,
): Promise<void> {
  const execution = activeExecution.value;
  const definitionId = execution?.task_definition_id;
  const approval = item.approval;
  const risk = item.risk_summary;
  if (
    !execution ||
    !definitionId ||
    !item.execution_plan_id ||
    !approval ||
    approval.state !== 'PENDING' ||
    !risk ||
    decidingApproval.value[item.id]
  ) {
    return;
  }
  const actionKinds = risk.action_kinds.join(', ') || '无';
  const reasons = risk.reason_codes.join(', ') || '无';
  const bytes = formatBytes(risk.estimated_download_bytes_upper_bound);
  const body = approve
    ? '确认批准当前高风险执行计划？\n\nAction：' +
      actionKinds +
      '\n风险原因：' +
      reasons +
      '\n预计最多下载：' +
      bytes +
      '\n\n审批仅绑定当前 Plan digest；计划变化后需重新审批。'
    : '确认拒绝当前高风险执行计划？\n\nAction：' +
      actionKinds +
      '\n风险原因：' +
      reasons +
      '\n\n拒绝后该 Plan 的审批决定不可覆盖。';
  try {
    await ElMessageBox.confirm(body, approve ? '批准高风险计划' : '拒绝高风险计划', {
      type: approve ? 'warning' : 'error',
      confirmButtonText: approve ? '确认批准' : '确认拒绝',
      cancelButtonText: '取消',
    });
  } catch (caught) {
    if (caught === 'cancel' || caught === 'close') return;
    throw caught;
  }

  decidingApproval.value = { ...decidingApproval.value, [item.id]: true };
  try {
    const updated = await decideTaskDefinitionExecutionApproval(
      definitionId,
      execution.id,
      item.id,
      {
        execution_plan_id: item.execution_plan_id,
        decision: approve ? 'APPROVE' : 'REJECT',
      },
      'approval-' + execution.id + '-' + item.id + '-' + crypto.randomUUID(),
    );
    activeExecution.value = updated;
    if (detailDefinition.value?.id === definitionId) await loadExecutionHistory();
    await refresh();
    ElMessage.success(approve ? '当前 Plan 已批准并继续安全执行链' : '当前 Plan 已拒绝');
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    const next = { ...decidingApproval.value };
    delete next[item.id];
    decidingApproval.value = next;
  }
}
</script>

<template>
  <section class="definition-center">
    <div class="section-toolbar">
      <div>
        <h2>任务中心</h2>
        <p>任务定义与每次执行分离；扫描、计划、授权、执行与校验逐步收口到统一安全生命周期。</p>
      </div>
      <div class="toolbar-actions">
        <el-button :loading="loading" @click="refresh"><RefreshCw :size="15" />刷新</el-button>
        <el-button type="primary" @click="openCreate()"><Plus :size="15" />新增任务</el-button>
      </div>
    </div>

    <div class="task-kind-cards">
      <button
        :class="['kind-card', { active: activeKind === 'MANUAL' }]"
        @click="activeKind = 'MANUAL'"
      >
        <span class="kind-icon"><FolderSearch :size="21" /></span>
        <span>
          <b>手动拆包任务</b>
          <small>
            运行中 {{ manualStats.running }} · 等待 {{ manualStats.waiting }} · 失败
            {{ manualStats.failed }} · 今日完成 {{ manualStats.completedToday }}
          </small>
        </span>
        <strong>{{ manualCount }}</strong>
      </button>
      <button
        :class="['kind-card', { active: activeKind === 'MONITOR' }]"
        @click="activeKind = 'MONITOR'"
      >
        <span class="kind-icon"><Clock3 :size="21" /></span>
        <span>
          <b>监控拆包任务</b>
          <small>
            启用 {{ monitorStats.enabled }} · 暂停 {{ monitorStats.paused }} · 异常
            {{ monitorStats.error }} · 今日触发 {{ monitorStats.triggeredToday }}
          </small>
        </span>
        <strong>{{ monitorCount }}</strong>
      </button>
    </div>

    <div class="table-card" v-loading="loading">
      <div class="table-card-head">
        <div>
          <b>{{ kindLabel(activeKind) }}</b>
          <span>{{ visibleDefinitions.length }} 个任务定义</span>
        </div>
      </div>
      <el-empty v-if="!loading && !visibleDefinitions.length" description="暂无任务定义" />
      <el-table v-else :data="visibleDefinitions" class="definition-table">
        <el-table-column label="任务名称" min-width="190">
          <template #default="scope">
            <div class="primary-cell">
              <b>{{ scope.row.name }}</b
              ><small>{{ scope.row.id }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="来源" min-width="160">
          <template #default="scope">
            <div class="primary-cell">
              <b>{{ sourceText(scope.row) }}</b>
              <small>{{ scope.row.source_kind === 'DOWNLOADER' ? '下载器' : '目录' }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="扫描站点" min-width="150">
          <template #default="scope">
            <div class="primary-cell">
              <b>{{ scope.row.site_name || '站点已删除' }}</b>
              <small :class="{ warning: !scope.row.site_available }">
                {{ scope.row.site_available ? '可用' : '不可用' }}
              </small>
            </div>
          </template>
        </el-table-column>
        <el-table-column v-if="activeKind === 'MONITOR'" label="执行时间" min-width="170">
          <template #default="scope">
            <div class="primary-cell">
              <code>{{ scope.row.cron_expression }}</code
              ><small>
                {{ scope.row.timezone }} · 下次
                {{ scope.row.next_run_at ? formatTime(scope.row.next_run_at) : '待计算' }}
              </small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="scope">
            <el-tag :type="statusTag(scope.row)" effect="light">{{
              statusLabel(scope.row)
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="执行结果" min-width="170">
          <template #default="scope">
            <span>{{ resultText(scope.row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="当前阶段" min-width="120">
          <template #default="scope">
            <span>{{ phaseText(scope.row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="输出" min-width="160">
          <template #default="scope">
            <div class="primary-cell">
              <b>{{ scope.row.output_directory }}</b
              ><small>{{ storageLabel(scope.row.storage_mode) }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="activeKind === 'MONITOR' ? '最近扫描' : '最近执行'" width="145">
          <template #default="scope">{{
            formatOptionalTime(recentActivityTime(scope.row))
          }}</template>
        </el-table-column>
        <el-table-column label="操作" width="330" fixed="right">
          <template #default="scope">
            <el-button link type="primary" @click="openDefinitionDetail(scope.row)">查看</el-button>
            <el-button
              v-if="scope.row.kind === 'MANUAL'"
              link
              type="success"
              :loading="Boolean(executing[scope.row.id])"
              :disabled="scope.row.status !== 'ENABLED'"
              @click="executeDefinition(scope.row)"
            >
              执行
            </el-button>
            <el-button
              v-if="scope.row.kind === 'MONITOR'"
              link
              type="primary"
              :loading="Boolean(scanning[scope.row.id])"
              :disabled="scope.row.status !== 'ENABLED'"
              @click="scanDefinition(scope.row)"
            >
              立即扫描
            </el-button>
            <el-button
              v-if="scope.row.kind === 'MONITOR'"
              link
              :type="scope.row.status === 'PAUSED' ? 'success' : 'info'"
              :loading="Boolean(togglingPause[scope.row.id])"
              :disabled="scope.row.status === 'SITE_UNAVAILABLE' || scope.row.status === 'ERROR'"
              @click="toggleMonitorPaused(scope.row)"
            >
              {{ scope.row.status === 'PAUSED' ? '恢复' : '暂停' }}
            </el-button>
            <el-button
              link
              type="warning"
              :loading="Boolean(retrying[scope.row.id])"
              :disabled="
                !scope.row.latest_execution || scope.row.latest_execution.failed_count === 0
              "
              @click="retryFailed(scope.row)"
            >
              <RotateCcw :size="13" />重试
            </el-button>
            <el-button link @click="openEdit(scope.row)">编辑</el-button>
            <el-button link @click="openClone(scope.row)">克隆</el-button>
            <el-button link type="danger" @click="removeDefinition(scope.row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="editingDefinitionId ? '编辑拆包任务' : '新增拆包任务'"
      width="min(900px, 94vw)"
      top="5vh"
      destroy-on-close
    >
      <div class="create-form">
        <div class="form-section">
          <h3>基本信息</h3>
          <div class="form-grid two">
            <el-form-item label="任务名称" required>
              <el-input v-model="draft.name" maxlength="120" placeholder="请输入任务名称" />
            </el-form-item>
            <el-form-item label="任务类型" required>
              <el-radio-group v-model="draft.kind" :disabled="Boolean(editingDefinitionId)">
                <el-radio-button value="MANUAL">手动拆包</el-radio-button>
                <el-radio-button value="MONITOR">监控拆包</el-radio-button>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="扫描站点" required>
              <el-select v-model="draft.siteId" placeholder="选择已配置站点" filterable>
                <el-option
                  v-for="site in configuredSites"
                  :key="site.id"
                  :label="siteLabel(site)"
                  :value="site.id"
                />
              </el-select>
            </el-form-item>
            <el-form-item v-if="draft.kind === 'MONITOR'" label="执行时间" required>
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
                    <template v-if="!['EVERY_MINUTES'].includes(cronVisualKind)">
                      <el-input-number v-model="cronVisualHour" :min="0" :max="23" />
                      <span>:</span>
                      <el-input-number v-model="cronVisualMinute" :min="0" :max="59" />
                    </template>
                  </div>
                  <el-input v-model="draft.cronExpression" readonly />
                </template>
                <el-input v-else v-model="draft.cronExpression" placeholder="例如：0 */2 * * *" />
                <small v-if="cronPreviewLoading" class="field-hint">正在计算执行时间…</small>
                <small v-else-if="cronPreviewError" class="field-error">{{
                  cronPreviewError
                }}</small>
                <div v-else-if="cronPreview" class="cron-preview">
                  <small class="field-hint">
                    {{ cronPreview.description }} · 时区 {{ cronPreview.timezone }}
                  </small>
                  <small class="field-hint">
                    未来 5 次：{{
                      cronPreview.next_runs.map((item) => formatTime(item)).join(' · ')
                    }}
                  </small>
                </div>
              </div>
            </el-form-item>
          </div>
        </div>

        <div class="form-section">
          <h3>来源</h3>
          <el-radio-group v-model="draft.sourceKind" class="source-kind-switch">
            <el-radio-button value="DOWNLOADER">下载器</el-radio-button>
            <el-radio-button value="DIRECTORY">目录</el-radio-button>
          </el-radio-group>
          <el-form-item v-if="draft.sourceKind === 'DOWNLOADER'" label="来源下载器" required>
            <el-select v-model="draft.downloaderId" placeholder="选择已配置下载器" filterable>
              <el-option
                v-for="item in configuredDownloaders"
                :key="item.id"
                :label="`${item.name} · ${item.enabled ? '已启用' : '已停用'}`"
                :value="item.id"
              />
            </el-select>
            <div v-if="draft.kind === 'MANUAL'" class="torrent-picker-summary">
              <el-button :disabled="!draft.downloaderId" @click="openTorrentDrawer">
                选择拆包种子
              </el-button>
              <span v-if="selectedTorrentCount">
                已选择 {{ selectedTorrentCount }} 个 · {{ formatBytes(selectedTorrentSize) }}
              </span>
              <span v-else>尚未选择真实种子</span>
            </div>
            <small v-else class="field-hint">监控任务将在调度阶段按来源规则发现新增种子。</small>
          </el-form-item>
          <template v-else>
            <el-form-item label="来源目录" required>
              <div class="directory-field">
                <el-input
                  v-model="draft.directoryPath"
                  placeholder="相对于 /data，例如 downloads/movies"
                />
                <el-button @click="openDirectoryDrawer('source')">选择目录</el-button>
                <el-button
                  v-if="draft.kind === 'MANUAL'"
                  type="primary"
                  plain
                  :loading="directoryPreviewLoading"
                  @click="scanDirectoryPreview"
                >
                  扫描预览
                </el-button>
              </div>
              <small v-if="draft.kind === 'MANUAL'" class="field-hint">
                <template v-if="directoryPreview">
                  匹配 {{ directoryPreview.matched_count }} 个 · 已选择
                  {{ selectedDirectoryPaths.length }} 个 · {{ formatBytes(selectedDirectorySize) }}
                </template>
                <template v-else>保存前必须扫描并确认要处理的文件快照。</template>
              </small>
            </el-form-item>
            <el-form-item label="目标下载器" required>
              <el-select
                v-model="draft.targetDownloaderId"
                placeholder="选择后续校验/做种下载器"
                filterable
              >
                <el-option
                  v-for="item in configuredDownloaders"
                  :key="item.id"
                  :label="`${item.name} · ${item.enabled ? '已启用' : '已停用'}`"
                  :value="item.id"
                  :disabled="!item.enabled"
                />
              </el-select>
              <small class="field-hint"
                >目录来源没有来源下载器，安全执行计划必须显式绑定目标下载器。</small
              >
            </el-form-item>
          </template>
        </div>

        <div class="form-section">
          <h3>文件过滤</h3>
          <el-form-item label="文件类型">
            <el-checkbox-group v-model="draft.fileTypes">
              <el-checkbox value="VIDEO">视频</el-checkbox>
              <el-checkbox value="ARCHIVE" disabled>压缩包（执行器待接入）</el-checkbox>
              <el-checkbox value="ISO" disabled>ISO（执行器待接入）</el-checkbox>
              <el-checkbox value="OTHER" disabled>其他（执行器待接入）</el-checkbox>
            </el-checkbox-group>
            <small class="field-hint">
              v0.1.5 当前安全执行链仅执行视频文件；RAR 分卷、ISO 和其他类型不会作为可执行对象开放。
            </small>
          </el-form-item>
          <div class="form-grid two">
            <el-form-item label="最小大小（MB）">
              <el-input-number
                v-model="draft.minSizeMb"
                :min="0"
                :controls="false"
                placeholder="不限"
              />
            </el-form-item>
            <el-form-item label="最大大小（MB）">
              <el-input-number
                v-model="draft.maxSizeMb"
                :min="0"
                :controls="false"
                placeholder="不限"
              />
            </el-form-item>
            <el-form-item label="包含名称">
              <el-input v-model="draft.includeName" placeholder="留空表示不限" />
            </el-form-item>
            <el-form-item label="排除名称">
              <el-input v-model="draft.excludeNames" placeholder="sample, trailer" />
            </el-form-item>
          </div>
          <div class="inline-switches">
            <el-checkbox v-model="draft.ignoreTempFiles">忽略临时文件</el-checkbox>
            <el-checkbox v-model="draft.includeSubdirectories">包含子目录</el-checkbox>
          </div>
        </div>

        <div class="form-section">
          <h3>成功后处理</h3>
          <div class="form-grid two">
            <el-form-item label="输出目录" required>
              <div class="directory-field">
                <el-input v-model="draft.outputDirectory" placeholder="相对于 /data，例如 output" />
                <el-button @click="openDirectoryDrawer('output')">选择目录</el-button>
              </div>
            </el-form-item>
            <el-form-item label="存放方式">
              <el-select v-model="draft.storageMode">
                <el-option label="硬链接（默认）" value="HARDLINK" />
                <el-option label="软链接（安全执行器待接入）" value="SYMLINK" disabled />
                <el-option label="复制（安全执行器待接入）" value="COPY" disabled />
              </el-select>
            </el-form-item>
            <el-form-item label="文件冲突">
              <el-select v-model="draft.conflictPolicy">
                <el-option label="校验一致后复用，否则停止" value="VERIFY_REUSE_OR_STOP" />
                <el-option label="跳过（安全执行器待接入）" value="SKIP" disabled />
                <el-option label="自动重命名（安全执行器待接入）" value="RENAME" disabled />
                <el-option label="覆盖（安全执行器待接入）" value="OVERWRITE" disabled />
              </el-select>
            </el-form-item>
            <el-form-item label="目录结构">
              <el-switch v-model="draft.preserveStructure" active-text="保持原目录结构" disabled />
              <small class="field-hint">当前安全执行计划固定保持 torrent 原目录结构。</small>
            </el-form-item>
          </div>
        </div>

        <el-collapse class="advanced-collapse">
          <el-collapse-item title="高级执行规则" name="advanced">
            <div class="form-grid two">
              <el-form-item label="稳定检测">
                <el-switch v-model="draft.stabilityDetectionEnabled" active-text="启用" />
              </el-form-item>
              <el-form-item label="稳定等待（秒）">
                <el-input-number
                  v-model="draft.stabilityWaitSeconds"
                  :disabled="!draft.stabilityDetectionEnabled"
                  :min="0"
                  :max="86400"
                />
              </el-form-item>
              <el-form-item label="下载完成状态">
                <el-switch v-model="draft.onlyCompletedDownloads" active-text="仅处理已完成" />
              </el-form-item>
              <el-form-item label="首次扫描范围">
                <el-select v-model="draft.initialScope">
                  <el-option label="仅处理创建后新增对象（默认）" value="NEW_ONLY" />
                  <el-option label="首次扫描包含现有对象" value="INCLUDE_EXISTING" />
                </el-select>
              </el-form-item>
              <el-form-item label="修改静默窗口（秒）">
                <el-input-number v-model="draft.debounceSeconds" :min="0" :max="86400" />
                <small class="field-hint">目录监控按文件最后修改时间等待静默；默认 30 秒。</small>
              </el-form-item>
              <el-form-item label="Cron 重叠">
                <el-select v-model="draft.overlapPolicy">
                  <el-option label="上一次仍运行时跳过" value="SKIP" />
                  <el-option label="结束后补执行一次" value="RUN_ONCE_AFTER" />
                </el-select>
              </el-form-item>
              <el-form-item label="自动重试">
                <el-switch
                  v-model="draft.autoRetryEnabled"
                  active-text="最多 3 次（1/5/15 分钟）"
                />
              </el-form-item>
              <el-form-item label="最大自动重试">
                <el-input-number
                  v-model="draft.maxAutoRetries"
                  :disabled="!draft.autoRetryEnabled"
                  :min="0"
                  :max="3"
                />
              </el-form-item>
              <el-form-item v-if="draft.kind === 'MONITOR'" label="高风险预授权">
                <div>
                  <el-switch
                    v-model="draft.highRiskPreauthorizationEnabled"
                    active-text="按 action 白名单预授权"
                  />
                  <small class="field-hint">
                    默认关闭。只会批准白名单明确覆盖的 HIGH 风险 action；未知或新增 action
                    仍需人工审批。
                  </small>
                </div>
              </el-form-item>
              <el-form-item v-if="draft.kind === 'MONITOR'" label="预授权 Action">
                <div>
                  <el-input
                    v-model="draft.highRiskAllowedActionKinds"
                    :disabled="!draft.highRiskPreauthorizationEnabled"
                    placeholder="例如 FUTURE_HIGH_RISK_ACTION，多个用逗号分隔"
                  />
                  <small class="field-hint">
                    使用后端 Execution Plan 的 action kind；审批仍绑定具体 Plan
                    digest，不是永久放开任务。
                  </small>
                </div>
              </el-form-item>
            </div>
          </el-collapse-item>
        </el-collapse>
      </div>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveDefinition">
          {{ editingDefinitionId ? '保存修改' : '保存任务定义' }}
        </el-button>
      </template>
    </el-dialog>

    <el-drawer
      v-model="definitionDrawerVisible"
      :title="detailDefinition ? `任务详情 · ${detailDefinition.name}` : '任务详情'"
      size="min(1120px, 96vw)"
      append-to-body
    >
      <template v-if="detailDefinition">
        <div class="definition-detail-head">
          <div>
            <el-tag :type="statusTag(detailDefinition)" effect="light">
              {{ statusLabel(detailDefinition) }}
            </el-tag>
            <span>{{ kindLabel(detailDefinition.kind) }}</span>
          </div>
          <div class="definition-detail-actions">
            <el-button
              v-if="detailDefinition.kind === 'MANUAL'"
              type="primary"
              :disabled="detailDefinition.status !== 'ENABLED'"
              @click="executeDefinition(detailDefinition)"
            >
              执行
            </el-button>
            <el-button
              v-if="detailDefinition.kind === 'MONITOR'"
              type="primary"
              :disabled="detailDefinition.status !== 'ENABLED'"
              @click="scanDefinition(detailDefinition)"
            >
              立即扫描
            </el-button>
            <el-button
              v-if="detailDefinition.kind === 'MONITOR'"
              :disabled="
                detailDefinition.status === 'SITE_UNAVAILABLE' ||
                detailDefinition.status === 'ERROR'
              "
              @click="toggleMonitorPaused(detailDefinition)"
            >
              {{ detailDefinition.status === 'PAUSED' ? '恢复' : '暂停' }}
            </el-button>
          </div>
        </div>

        <el-tabs v-model="detailTab" class="definition-detail-tabs">
          <el-tab-pane label="概览" name="overview">
            <div class="detail-overview-grid">
              <div>
                <small>扫描站点</small><b>{{ detailDefinition.site_name || '站点已删除' }}</b>
              </div>
              <div>
                <small>来源</small><b>{{ sourceText(detailDefinition) }}</b>
              </div>
              <div>
                <small>调度规则</small>
                <b>{{ detailDefinition.cron_expression || '手动执行' }}</b>
              </div>
              <div>
                <small>下次执行</small>
                <b>{{ formatOptionalTime(detailDefinition.next_run_at) }}</b>
              </div>
              <div>
                <small>当前状态</small><b>{{ statusLabel(detailDefinition) }}</b>
              </div>
              <div>
                <small>最近执行</small><b>{{ resultText(detailDefinition) }}</b>
              </div>
              <div>
                <small>输出目录</small><b>{{ detailDefinition.output_directory }}</b>
              </div>
              <div>
                <small>存放方式</small><b>{{ storageLabel(detailDefinition.storage_mode) }}</b>
              </div>
            </div>
          </el-tab-pane>

          <el-tab-pane label="配置" name="config">
            <div class="detail-config-grid">
              <section>
                <h4>来源与过滤</h4>
                <p>来源类型：{{ detailDefinition.source_kind }}</p>
                <p>文件类型：{{ detailDefinition.file_types.join(', ') }}</p>
                <p>视频扩展名：{{ detailDefinition.video_extensions.join(', ') }}</p>
                <p>排除名称：{{ detailDefinition.exclude_names.join(', ') || '无' }}</p>
                <p>包含子目录：{{ detailDefinition.include_subdirectories ? '是' : '否' }}</p>
              </section>
              <section>
                <h4>输出策略</h4>
                <p>输出目录：{{ detailDefinition.output_directory }}</p>
                <p>存放方式：{{ storageLabel(detailDefinition.storage_mode) }}</p>
                <p>保持目录结构：{{ detailDefinition.preserve_structure ? '是' : '否' }}</p>
                <p>冲突策略：{{ detailDefinition.conflict_policy }}</p>
              </section>
              <section>
                <h4>执行策略</h4>
                <p>
                  稳定检测：{{ detailDefinition.stability_detection_enabled ? '开启' : '关闭' }}
                </p>
                <p>稳定等待：{{ detailDefinition.stability_wait_seconds }} 秒</p>
                <p>首次扫描：{{ detailDefinition.initial_scope }}</p>
                <p>重叠策略：{{ detailDefinition.overlap_policy }}</p>
                <p>
                  自动重试：{{ detailDefinition.auto_retry_enabled ? '开启' : '关闭' }} · 最多
                  {{ detailDefinition.max_auto_retries }} 次
                </p>
                <p v-if="detailDefinition.kind === 'MONITOR'">
                  高风险预授权：{{
                    detailDefinition.high_risk_preauthorization_enabled ? '开启' : '关闭'
                  }}
                </p>
                <p
                  v-if="
                    detailDefinition.kind === 'MONITOR' &&
                    detailDefinition.high_risk_preauthorization_enabled
                  "
                >
                  Action 白名单：{{
                    detailDefinition.high_risk_allowed_action_kinds.join(', ') || '未配置'
                  }}
                </p>
              </section>
              <section>
                <h4>来源快照配置</h4>
                <pre>{{ prettyJson(detailDefinition.source_config) }}</pre>
              </section>
            </div>
          </el-tab-pane>

          <el-tab-pane label="执行记录" name="executions">
            <div class="execution-history-toolbar">
              <el-input
                v-model="executionSearch"
                clearable
                placeholder="搜索源文件 / 种子名称"
                @keyup.enter="
                  executionHistoryPage = 1;
                  loadExecutionHistory();
                "
                @clear="
                  executionHistoryPage = 1;
                  loadExecutionHistory();
                "
              />
              <el-select
                v-model="executionStatusFilter"
                clearable
                placeholder="状态"
                @change="
                  executionHistoryPage = 1;
                  loadExecutionHistory();
                "
              >
                <el-option label="等待" value="PENDING" />
                <el-option label="运行中" value="RUNNING" />
                <el-option label="完成" value="COMPLETED" />
                <el-option label="部分失败" value="PARTIAL_FAILED" />
                <el-option label="失败" value="FAILED" />
              </el-select>
              <el-select
                v-model="executionTriggerFilter"
                clearable
                placeholder="触发方式"
                @change="
                  executionHistoryPage = 1;
                  loadExecutionHistory();
                "
              >
                <el-option label="手动执行" value="MANUAL" />
                <el-option label="Cron" value="CRON" />
                <el-option label="立即扫描" value="IMMEDIATE_SCAN" />
                <el-option label="失败重试" value="FAILED_RETRY" />
                <el-option label="系统恢复" value="SYSTEM_RECOVERY" />
              </el-select>
              <el-date-picker
                v-model="executionTimeRange"
                type="datetimerange"
                start-placeholder="开始时间"
                end-placeholder="结束时间"
                range-separator="至"
                @change="
                  executionHistoryPage = 1;
                  loadExecutionHistory();
                "
              />
              <el-button :loading="executionHistoryLoading" @click="loadExecutionHistory"
                >刷新</el-button
              >
            </div>

            <el-table :data="executionHistory" v-loading="executionHistoryLoading">
              <el-table-column label="执行时间" min-width="145">
                <template #default="scope">{{ formatOptionalTime(scope.row.started_at) }}</template>
              </el-table-column>
              <el-table-column label="触发方式" width="110">
                <template #default="scope">{{ triggerLabel(scope.row.trigger) }}</template>
              </el-table-column>
              <el-table-column label="发现" width="72" prop="discovered_count" />
              <el-table-column label="成功" width="72" prop="success_count" />
              <el-table-column label="失败" width="72" prop="failed_count" />
              <el-table-column label="跳过" width="72" prop="skipped_count" />
              <el-table-column label="状态" width="120" prop="status" />
              <el-table-column label="耗时" width="120">
                <template #default="scope">{{ executionDuration(scope.row) }}</template>
              </el-table-column>
              <el-table-column label="操作" width="150" fixed="right">
                <template #default="scope">
                  <el-button link type="primary" @click="openExecutionDetail(scope.row.id)"
                    >查看</el-button
                  >
                  <el-button
                    link
                    type="warning"
                    :disabled="scope.row.failed_count === 0"
                    @click="retryExecution(scope.row)"
                  >
                    重试失败
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
            <div class="execution-history-pagination">
              <el-pagination
                v-model:current-page="executionHistoryPage"
                v-model:page-size="executionHistoryPageSize"
                :total="executionHistoryTotal"
                :page-sizes="[10, 20, 50, 100]"
                layout="total, sizes, prev, pager, next"
                @current-change="loadExecutionHistory"
                @size-change="
                  executionHistoryPage = 1;
                  loadExecutionHistory();
                "
              />
            </div>
          </el-tab-pane>
        </el-tabs>
      </template>
    </el-drawer>

    <el-drawer
      v-model="executionDrawerVisible"
      title="执行记录详情"
      size="min(980px, 94vw)"
      append-to-body
    >
      <template v-if="activeExecution">
        <el-alert
          v-if="activeExecution.status === 'PENDING'"
          title="已进入安全执行链，尚未执行副作用"
          description="可使用“推进生命周期”继续只读分析与计划生成；候选证据需要人工确认时会停在审核边界，高风险动作也会在副作用前停止。"
          type="success"
          :closable="false"
          show-icon
        />
        <div class="execution-summary-grid">
          <div>
            <small>执行 ID</small><code>{{ activeExecution.id }}</code>
          </div>
          <div>
            <small>状态 / 阶段</small
            ><b>{{ activeExecution.status }} / {{ activeExecution.phase }}</b>
          </div>
          <div>
            <small>触发方式</small><b>{{ triggerLabel(activeExecution.trigger) }}</b>
          </div>
          <div>
            <small>耗时</small><b>{{ executionDuration(activeExecution) }}</b>
          </div>
          <div>
            <small>发现对象</small><b>{{ activeExecution.discovered_count }}</b>
          </div>
          <div>
            <small>结果</small>
            <b>
              成功 {{ activeExecution.success_count }} · 失败 {{ activeExecution.failed_count }} ·
              跳过
              {{ activeExecution.skipped_count }}
            </b>
          </div>
          <div>
            <small>Trace ID</small><code>{{ activeExecution.trace_id }}</code>
          </div>
          <div>
            <small>扫描站点</small>
            <b>{{
              detailDefinition?.site_name || String(activeExecution.config_snapshot.site_id || '—')
            }}</b>
          </div>
          <div>
            <small>来源执行 ID</small><code>{{ activeExecution.source_execution_id || '—' }}</code>
          </div>
          <div>
            <small>开始 / 结束</small>
            <b>
              {{ formatOptionalTime(activeExecution.started_at) }} /
              {{ formatOptionalTime(activeExecution.finished_at) }}
            </b>
          </div>
        </div>
        <el-tabs class="execution-detail-tabs">
          <el-tab-pane label="执行对象">
            <el-table :data="activeExecution.items" class="execution-items-table">
              <el-table-column label="对象" min-width="220">
                <template #default="scope">
                  <div class="primary-cell">
                    <b>{{ scope.row.name }}</b>
                    <small>{{ scope.row.source }}</small>
                    <small>{{ scope.row.source_object_key }}</small>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="大小" width="105">
                <template #default="scope">
                  {{ scope.row.size_bytes === null ? '—' : formatBytes(scope.row.size_bytes) }}
                </template>
              </el-table-column>
              <el-table-column label="生命周期" min-width="155">
                <template #default="scope">
                  <div class="primary-cell">
                    <b>{{ scope.row.lifecycle_stage }}</b>
                    <small>{{ scope.row.phase }}</small>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="风险 / 授权" min-width="170">
                <template #default="scope">
                  <div class="primary-cell">
                    <b>{{ scope.row.risk_level }}</b>
                    <small>{{ scope.row.authorization_status }}</small>
                    <small v-if="scope.row.risk_summary">
                      {{ scope.row.risk_summary.reason_codes.join(', ') }}
                    </small>
                    <small v-if="scope.row.execution_plan_id">
                      Plan: {{ scope.row.execution_plan_id }} ·
                      {{ scope.row.execution_plan_ready ? 'READY' : 'NOT_READY' }}
                    </small>
                    <small v-if="scope.row.approval">
                      Approval: {{ scope.row.approval.state }}
                      <template v-if="scope.row.approval.decision_source">
                        · {{ scope.row.approval.decision_source }}
                      </template>
                    </small>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="校验 / 收尾" min-width="190">
                <template #default="scope">
                  <div class="primary-cell">
                    <b>{{ scope.row.closure.status }}</b>
                    <small>
                      FS {{ scope.row.closure.filesystem_status }} · DL
                      {{ scope.row.closure.downloader_status }}
                    </small>
                    <small v-if="scope.row.closure.operation_attention_count">
                      Attention {{ scope.row.closure.operation_attention_count }} · Reconcile
                      {{ scope.row.closure.reconcile_required_count }} · Blocked
                      {{ scope.row.closure.rollback_blocked_count }}
                    </small>
                    <small v-if="scope.row.closure.retention_candidate_count">
                      Retention candidate {{ scope.row.closure.retention_candidate_count }}
                    </small>
                    <small v-if="scope.row.closure.issue_codes.length" class="execution-error">
                      {{ scope.row.closure.issue_codes.join(', ') }}
                    </small>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="进度" width="85">
                <template #default="scope">{{
                  scope.row.progress === null ? '—' : `${scope.row.progress}%`
                }}</template>
              </el-table-column>
              <el-table-column label="结果 / 错误" min-width="210">
                <template #default="scope">
                  {{
                    scope.row.result ||
                    (scope.row.authorization_status === 'REVIEW_REQUIRED'
                      ? '等待候选确认'
                      : scope.row.authorization_status === 'APPROVAL_REQUIRED'
                        ? '等待高风险授权'
                        : '处理中')
                  }}
                  <small v-if="scope.row.error_summary_zh" class="execution-error">
                    {{ scope.row.error_code }} · {{ scope.row.error_summary_zh }}
                  </small>
                  <small v-if="scope.row.technical_detail" class="execution-technical-detail">
                    {{ scope.row.technical_detail }}
                  </small>
                  <small v-if="scope.row.retry_count">已重试 {{ scope.row.retry_count }} 次</small>
                </template>
              </el-table-column>
              <el-table-column label="底层 Run" min-width="220">
                <template #default="scope">
                  <code v-if="scope.row.unpack_task_id">{{ scope.row.unpack_task_id }}</code>
                  <span v-else>—</span>
                </template>
              </el-table-column>
              <el-table-column label="审批" min-width="155" fixed="right">
                <template #default="scope">
                  <div
                    v-if="
                      scope.row.authorization_status === 'APPROVAL_REQUIRED' &&
                      scope.row.approval?.state === 'PENDING'
                    "
                    class="toolbar-actions"
                  >
                    <el-button
                      link
                      type="primary"
                      :loading="Boolean(decidingApproval[scope.row.id])"
                      @click="decideApproval(scope.row, true)"
                    >
                      批准
                    </el-button>
                    <el-button
                      link
                      type="danger"
                      :disabled="Boolean(decidingApproval[scope.row.id])"
                      @click="decideApproval(scope.row, false)"
                    >
                      拒绝
                    </el-button>
                  </div>
                  <span v-else-if="scope.row.approval">
                    {{ scope.row.approval.state }}
                  </span>
                  <span v-else>—</span>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
          <el-tab-pane label="审核 / 对账">
            <div v-if="executionEvidenceItems.length" class="execution-evidence-selector">
              <span>当前 Run</span>
              <el-select v-model="executionEvidenceTaskId" filterable>
                <el-option
                  v-for="item in executionEvidenceItems"
                  :key="item.itemId"
                  :label="`${item.name} · ${item.closure.status}`"
                  :value="item.taskId"
                />
              </el-select>
            </div>
            <TaskExecutionEvidencePanel
              v-if="executionEvidenceTaskId"
              :key="executionEvidenceTaskId"
              :task-id="executionEvidenceTaskId"
            />
            <el-empty v-else description="当前执行没有可关联的底层安全 Run" />
          </el-tab-pane>
          <el-tab-pane label="执行时间线">
            <el-timeline class="execution-timeline">
              <el-timeline-item
                v-for="event in activeExecution.events"
                :key="event.id"
                :timestamp="formatTime(event.created_at)"
                placement="top"
              >
                <b>{{ taskEventTranslation(event.event_code).title }}</b>
                <p>{{ event.message }}</p>
                <small>
                  {{ event.event_code }}
                  <template v-if="!taskEventTranslation(event.event_code).translated">
                    · 未翻译事件</template
                  >
                </small>
                <pre v-if="Object.keys(event.context).length">{{ prettyJson(event.context) }}</pre>
              </el-timeline-item>
            </el-timeline>
            <el-empty v-if="!activeExecution.events.length" description="暂无结构化事件" />
          </el-tab-pane>
          <el-tab-pane label="配置快照">
            <pre class="config-snapshot">{{ prettyJson(activeExecution.config_snapshot) }}</pre>
          </el-tab-pane>
        </el-tabs>
        <div class="execution-drawer-actions">
          <el-button @click="openExecutionLogs">查看关联日志</el-button>
          <el-button
            type="primary"
            :loading="advancingExecution"
            :disabled="
              ['COMPLETED', 'PARTIAL_FAILED', 'FAILED', 'CANCELLED'].includes(
                activeExecution.status,
              )
            "
            @click="advanceActiveExecution"
          >
            推进生命周期
          </el-button>
          <el-button
            type="warning"
            :disabled="activeExecution.failed_count === 0"
            @click="retryActiveExecution"
          >
            重试失败对象
          </el-button>
        </div>
      </template>
    </el-drawer>

    <el-drawer
      v-model="directoryDrawerVisible"
      :title="directoryTarget === 'source' ? '选择来源目录' : '选择输出目录'"
      size="min(760px, 94vw)"
      append-to-body
    >
      <div class="directory-browser" v-loading="directoryBrowserLoading">
        <div class="directory-browser-toolbar">
          <code>/data/{{ directoryBrowserPath === '.' ? '' : directoryBrowserPath }}</code>
          <el-button
            :disabled="directoryParentPath === null"
            @click="directoryParentPath && loadDirectoryEntries(directoryParentPath)"
          >
            返回上级
          </el-button>
          <el-button type="primary" @click="chooseCurrentDirectory">选择当前目录</el-button>
        </div>
        <el-table :data="directoryEntries" height="calc(100vh - 230px)">
          <el-table-column label="目录" min-width="360">
            <template #default="scope">
              <div class="primary-cell">
                <b>{{ scope.row.name }}</b
                ><small>{{ scope.row.path }}</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="110">
            <template #default="scope">
              <el-button link type="primary" @click="loadDirectoryEntries(scope.row.path)"
                >进入</el-button
              >
            </template>
          </el-table-column>
        </el-table>
        <el-empty
          v-if="!directoryBrowserLoading && !directoryEntries.length"
          description="当前目录没有子目录"
        />
      </div>
    </el-drawer>

    <el-drawer
      v-model="directoryPreviewVisible"
      title="目录扫描预览"
      size="min(980px, 94vw)"
      append-to-body
    >
      <template v-if="directoryPreview">
        <el-alert
          :title="`匹配 ${directoryPreview.matched_count} 个文件 · ${formatBytes(directoryPreview.total_size_bytes)}`"
          description="保存任务时会固化已勾选文件的路径、大小、inode 与 mtime；执行前如有变化将阻断该对象。"
          type="info"
          :closable="false"
          show-icon
        />
        <el-table :data="directoryPreview.files" class="directory-preview-table">
          <el-table-column label="选择" width="72">
            <template #default="scope">
              <el-checkbox
                :model-value="directoryFileSelected(scope.row)"
                @change="toggleDirectoryFile(scope.row, Boolean($event))"
              />
            </template>
          </el-table-column>
          <el-table-column label="文件" min-width="420" prop="relative_path" />
          <el-table-column label="大小" width="130">
            <template #default="scope">{{ formatBytes(scope.row.size_bytes) }}</template>
          </el-table-column>
          <el-table-column label="mtime(ns)" min-width="190" prop="mtime_ns" />
        </el-table>
        <div class="directory-preview-actions">
          <span
            >已选择 {{ selectedDirectoryPaths.length }} 个 ·
            {{ formatBytes(selectedDirectorySize) }}</span
          >
          <el-button type="primary" @click="directoryPreviewVisible = false">确认选择</el-button>
        </div>
      </template>
    </el-drawer>

    <el-drawer
      v-model="torrentDrawerVisible"
      title="选择拆包种子"
      size="min(1180px, 94vw)"
      append-to-body
      destroy-on-close
    >
      <div class="torrent-drawer">
        <div class="torrent-toolbar">
          <el-input
            v-model="torrentSearch"
            clearable
            placeholder="搜索种子名称"
            @keyup.enter="
              torrentPage = 1;
              loadTorrents();
            "
            @clear="
              torrentPage = 1;
              loadTorrents();
            "
          />
          <el-button
            :loading="torrentLoading"
            @click="
              torrentPage = 1;
              loadTorrents();
            "
          >
            搜索
          </el-button>
          <span>已选择 {{ selectedTorrentCount }} 个 · {{ formatBytes(selectedTorrentSize) }}</span>
        </div>
        <div class="torrent-filter-grid">
          <el-input v-model="torrentStatus" clearable placeholder="状态，例如 uploading" />
          <el-input v-model="torrentCategory" clearable placeholder="分类" />
          <el-input v-model="torrentTag" clearable placeholder="标签" />
          <el-input v-model="torrentTracker" clearable placeholder="Tracker 关键字" />
          <el-input v-model="torrentSavePath" clearable placeholder="保存路径" />
          <el-button
            :loading="torrentLoading"
            @click="
              torrentPage = 1;
              loadTorrents();
            "
          >
            应用筛选
          </el-button>
        </div>

        <el-table :data="torrentItems" v-loading="torrentLoading" height="calc(100vh - 250px)">
          <el-table-column label="选择" width="72" fixed="left">
            <template #default="scope">
              <el-checkbox
                :model-value="torrentSelected(scope.row)"
                @change="toggleTorrent(scope.row, Boolean($event))"
              />
            </template>
          </el-table-column>
          <el-table-column label="种子名称" min-width="260">
            <template #default="scope">
              <div class="primary-cell">
                <b>{{ scope.row.name }}</b>
                <small>{{ scope.row.torrent_hash }}</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="状态" min-width="120" prop="status" />
          <el-table-column label="完成度" width="105">
            <template #default="scope">{{ Math.round(scope.row.progress * 100) }}%</template>
          </el-table-column>
          <el-table-column label="大小" width="110">
            <template #default="scope">{{ formatBytes(scope.row.size_bytes) }}</template>
          </el-table-column>
          <el-table-column label="分类" min-width="110">
            <template #default="scope">{{ scope.row.category || '—' }}</template>
          </el-table-column>
          <el-table-column label="标签" min-width="170">
            <template #default="scope">{{ scope.row.tags.join(', ') || '—' }}</template>
          </el-table-column>
          <el-table-column label="Tracker" min-width="200">
            <template #default="scope">{{ scope.row.tracker || '—' }}</template>
          </el-table-column>
          <el-table-column label="保存路径" min-width="260" prop="save_path" />
        </el-table>

        <div class="torrent-pagination">
          <el-pagination
            v-model:current-page="torrentPage"
            v-model:page-size="torrentPageSize"
            :total="torrentTotal"
            :page-sizes="[25, 50, 100, 200]"
            layout="total, sizes, prev, pager, next"
            @current-change="loadTorrents"
            @size-change="
              torrentPage = 1;
              loadTorrents();
            "
          />
          <el-button type="primary" @click="torrentDrawerVisible = false">
            确认选择（{{ selectedTorrentCount }}）
          </el-button>
        </div>
      </div>
    </el-drawer>
  </section>
</template>

<style scoped>
.definition-center {
  display: grid;
  gap: 18px;
}
.section-toolbar,
.table-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.section-toolbar h2 {
  margin: 0 0 4px;
  font-size: 19px;
}
.section-toolbar p {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
}
.toolbar-actions {
  display: flex;
  gap: 8px;
}
.task-kind-cards {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
.kind-card {
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 12px;
  padding: 18px;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--surface);
  color: inherit;
  text-align: left;
  cursor: pointer;
  transition: 0.16s ease;
}
.kind-card:hover,
.kind-card.active {
  border-color: var(--blue);
  box-shadow: 0 8px 26px rgba(31, 93, 255, 0.08);
}
.kind-icon {
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  border-radius: 11px;
  background: var(--soft-blue);
  color: var(--blue);
}
.kind-card span:nth-child(2) {
  display: grid;
  gap: 4px;
}
.kind-card small {
  color: var(--muted);
}
.kind-card strong {
  font-size: 25px;
}
.table-card {
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--surface);
  overflow: hidden;
}
.table-card-head {
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
}
.table-card-head > div {
  display: flex;
  gap: 8px;
  align-items: baseline;
}
.table-card-head span {
  color: var(--muted);
  font-size: 11px;
}
.primary-cell {
  display: grid;
  gap: 3px;
}
.primary-cell small {
  color: var(--muted);
  font-size: 10px;
  overflow-wrap: anywhere;
}
.primary-cell small.warning {
  color: var(--warning);
}
.primary-cell code {
  font-size: 11px;
}
.create-form {
  max-height: 70vh;
  overflow: auto;
  padding-right: 6px;
  display: grid;
  gap: 16px;
}
.form-section {
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
}
.form-section h3 {
  margin: 0 0 14px;
  font-size: 14px;
}
.form-grid.two {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 18px;
}
.source-kind-switch {
  margin-bottom: 16px;
}
.inline-switches {
  display: flex;
  gap: 24px;
  padding-left: 102px;
}
.field-hint {
  display: block;
  margin-top: 6px;
  color: var(--muted);
  font-size: 11px;
}
.field-error {
  display: block;
  margin-top: 6px;
  color: var(--danger);
  font-size: 11px;
}
.cron-editor,
.cron-preview {
  width: 100%;
  display: grid;
  gap: 8px;
}
.cron-visual-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.cron-kind-select {
  min-width: 150px;
}
.directory-field {
  width: 100%;
  display: flex;
  gap: 8px;
}
.directory-field :deep(.el-input) {
  flex: 1;
}
.directory-browser,
.directory-preview-table {
  margin-top: 12px;
}
.directory-browser-toolbar,
.directory-preview-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.directory-browser-toolbar code {
  flex: 1;
  overflow-wrap: anywhere;
}
.directory-preview-actions {
  justify-content: flex-end;
  margin-top: 12px;
  color: var(--muted);
  font-size: 12px;
}
.directory-preview-actions span {
  margin-right: auto;
}
.torrent-picker-summary {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 8px;
  color: var(--muted);
  font-size: 11px;
}
.torrent-drawer {
  display: grid;
  gap: 14px;
  height: 100%;
}
.torrent-toolbar,
.torrent-pagination {
  display: flex;
  align-items: center;
  gap: 10px;
}
.torrent-toolbar :deep(.el-input) {
  max-width: 360px;
}
.torrent-toolbar span {
  margin-left: auto;
  color: var(--muted);
  font-size: 12px;
}
.torrent-filter-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}
.torrent-pagination {
  justify-content: space-between;
}
.execution-summary-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin: 16px 0;
}
.execution-summary-grid > div {
  display: grid;
  gap: 5px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
}
.execution-summary-grid small,
.execution-error {
  color: var(--muted);
  font-size: 11px;
}
.execution-error {
  display: block;
  margin-top: 4px;
  color: var(--danger);
}
.execution-drawer-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
.execution-evidence-selector {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}
.execution-evidence-selector > span {
  color: var(--muted);
  font-size: 12px;
}
.execution-evidence-selector :deep(.el-select) {
  min-width: min(520px, 70vw);
}
.definition-detail-head,
.definition-detail-head > div,
.definition-detail-actions,
.execution-history-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
}
.definition-detail-head {
  justify-content: space-between;
  margin-bottom: 12px;
}
.definition-detail-tabs,
.execution-detail-tabs {
  margin-top: 10px;
}
.detail-overview-grid,
.detail-config-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.detail-overview-grid > div,
.detail-config-grid section {
  display: grid;
  gap: 6px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
}
.detail-overview-grid small {
  color: var(--muted);
}
.detail-config-grid h4,
.detail-config-grid p {
  margin: 0;
}
.detail-config-grid p {
  color: var(--muted);
  font-size: 12px;
}
.detail-config-grid pre,
.config-snapshot,
.execution-timeline pre {
  margin: 0;
  padding: 10px;
  border-radius: 8px;
  background: var(--surface-soft);
  font-size: 11px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.execution-history-toolbar {
  margin-bottom: 12px;
}
.execution-history-toolbar :deep(.el-input) {
  max-width: 320px;
}
.execution-history-toolbar :deep(.el-select) {
  width: 150px;
}
.execution-history-pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
.execution-technical-detail {
  display: block;
  margin-top: 4px;
  color: var(--muted);
  font-size: 10px;
  overflow-wrap: anywhere;
}
.execution-timeline p {
  margin: 5px 0;
  color: var(--muted);
}
.advanced-collapse {
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 0 16px;
}
:deep(.el-form-item) {
  margin-bottom: 14px;
}
:deep(.el-form-item__label) {
  min-width: 102px;
}
:deep(.el-select),
:deep(.el-input-number) {
  width: 100%;
}
@media (max-width: 860px) {
  .task-kind-cards,
  .form-grid.two,
  .detail-overview-grid,
  .detail-config-grid {
    grid-template-columns: 1fr;
  }
  .section-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }
  .inline-switches {
    padding-left: 0;
    flex-wrap: wrap;
  }
  .definition-detail-head,
  .execution-history-toolbar {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
