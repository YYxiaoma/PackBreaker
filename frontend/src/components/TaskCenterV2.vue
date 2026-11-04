<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import {
  CheckCircle2,
  Clock3,
  Eye,
  FolderOpen,
  Info,
  Ellipsis,
  ExternalLink,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
} from '@lucide/vue';
import { ElMessage, ElMessageBox } from 'element-plus';

import { toApiProblem } from '../api/client';
import {
  listDownloaders,
  listDownloaderTorrents,
  type Downloader,
  type DownloaderTorrent,
} from '../api/downloaders';
import { listSites, type Site } from '../api/sites';
import {
  browseUnpackTree,
  createUnpackDefinition,
  deleteUnpackExecutionItem,
  deleteUnpackDefinition,
  createUnpackSourceScan,
  getUnpackExecution,
  getUnpackItemDiagnostics,
  getUnpackSourceScanSelectionSummary,
  listUnpackDefinitions,
  listUnpackTemplates,
  listUnpackExecutionItems,
  listUnpackExecutions,
  listUnpackItemCandidates,
  previewManualUnpackCandidate,
  addManualUnpackCandidate,
  listUnpackSourceScanItems,
  listUnpackTreeRoots,
  retryUnpackItemMatch,
  retryUnpackExecutionItems,
  reviewUnpackItem,
  runUnpackDefinition,
  updateUnpackSourceScanSelection,
  updateUnpackDefinition,
  type UnpackDefinition,
  type UnpackDefinitionCreate,
  type UnpackTemplate,
  type UnpackExecution,
  type UnpackExecutionItem,
  type UnpackItemDiagnostics,
  type UnpackItemStatus,
  type UnpackMatchCandidate,
  type UnpackMatchCandidateList,
  type ManualCandidatePreview,
  type ManualCandidateInput,
  type UnpackSourceScanItem,
  type UnpackTreeEntry,
} from '../api/unpack';
import { createClientNonce } from '../clientNonce';
import {
  unpackDefinitionStatusLabel,
  unpackExecutionStatusLabel,
  unpackItemExternalRiskLabel,
  unpackItemRetryAvailability,
  unpackReconciliationGuidance,
  unpackOperationTypeLabel,
  unpackOperationStatusLabel,
  unpackExecutionStageLabel,
  unpackItemStatusLabel,
  unpackItemStatusSummary,
  unpackPlanStatusLabel,
  unpackPlanBlockReasonLabel,
  unpackErrorLabel,
  unpackProgressCount,
  unpackProgressPercent,
  unpackSourceKindLabel,
  unpackTriggerLabel,
  unpackVerificationLevelLabel,
} from '../unpackPresentation';
import {
  unpackExecutionDetailTimeline,
  unpackExecutionDetailSummary,
  unpackExecutionDetailRecordNote,
} from '../unpackExecutionDetail';
import { createUnpackDiagnosticsGuard } from '../unpackDiagnosticsGuard';
type TriggerKind = 'MANUAL' | 'MONITOR';
type TaskKind = 'MANUAL' | 'MONITOR' | 'TEMPLATE';
type SourceKind = 'DIRECTORY' | 'DOWNLOADER';
type StorageMode = 'HARDLINK' | 'SYMLINK' | 'COPY';
type DirectoryTarget = 'SOURCE' | 'OUTPUT';

interface TaskRow {
  definition: UnpackDefinition;
  execution: UnpackExecution | null;
}

interface TreeNodeData {
  name: string;
  path: string;
  token: string;
}

interface Draft {
  name: string;
  triggerKind: TriggerKind;
  taskKind: TaskKind;
  templateId: string;
  templateVersion: number | null;
  sourceKind: SourceKind;
  sourceTorrentHash: string;
  siteIds: string[];
  sourceDirectory: string;
  sourceToken: string;
  sourceDownloaderId: string;
  downloaderNameContains: string;
  downloaderCategories: string;
  downloaderTags: string;
  outputDirectory: string;
  targetDownloaderId: string;
  targetCategory: string;
  targetTags: string[];
  extensions: string;
  minSizeMb: number | null;
  maxSizeMb: number | null;
  includeName: string;
  excludeNames: string;
  includeSubdirectories: boolean;
  storageMode: StorageMode;
  retryEnabled: boolean;
  maxRetries: number;
  autoMatchEnabled: boolean;
  autoMatchPercent: number;
  cronExpression: string;
}

const loading = ref(false);
const saving = ref(false);
const definitions = ref<UnpackDefinition[]>([]);
const executions = ref<UnpackExecution[]>([]);
const sites = ref<Site[]>([]);
const downloaders = ref<Downloader[]>([]);
const templates = ref<UnpackTemplate[]>([]);
const sourceTorrents = ref<DownloaderTorrent[]>([]);
const sourceTorrentsLoading = ref(false);
let sourceTorrentsRequestId = 0;
let sourceTorrentsLoadedFor = '';
let sourceTorrentsLoadingFor = '';

const typeFilter = ref('');
const statusFilter = ref('');
const keyword = ref('');
const appliedTypeFilter = ref('');
const appliedStatusFilter = ref('');
const appliedKeyword = ref('');

const createVisible = ref(false);
const unpackTableContainer = ref<HTMLElement | null>(null);
const unpackTableContainerWidth = ref(0);
const showInlineDefinitionActions = computed(() => unpackTableContainerWidth.value >= 1000);
const definitionActionColumnWidth = computed(() => (showInlineDefinitionActions.value ? 320 : 200));
let unpackTableResizeObserver: ResizeObserver | undefined;

const editingDefinition = ref<UnpackDefinition | null>(null);
const createStep = ref(1);
const advancedRulesExpanded = ref(false);
const standardWizardSteps = [
  {
    number: 1,
    label: '基本信息',
    title: '基本信息',
    description: '设置任务名称、拆包方式和扫描站点。',
  },
  {
    number: 2,
    label: '来源与目标',
    title: '来源与目标',
    description: '选择扫描来源、输出目录和最终辅种下载器。',
  },
  {
    number: 3,
    label: '文件过滤',
    title: '文件过滤',
    description: '设置需要处理的文件范围；高级执行规则默认收起。',
  },
  {
    number: 4,
    label: '确认创建',
    title: '确认创建',
    description: '检查全部配置，确认无误后创建任务。',
  },
] as const;
const templateWizardSteps = [
  {
    number: 1,
    label: '选择模板',
    title: '选择拆包模板',
    description: '选择已完成入库的模板及其目标站点。',
  },
  {
    number: 2,
    label: '来源与目标',
    title: '选择来源与目标',
    description: '选择一个大包目录或一个已完成的下载器种子。',
  },
  {
    number: 3,
    label: '概览确认',
    title: '检查模板与来源',
    description: '确认模板版本、扫描统计和输出目标；此步骤不执行匹配。',
  },
] as const;
const scopeConfirmVisible = ref(false);
const mediaSelectVisible = ref(false);
const directoryPickerVisible = ref(false);
const directoryPickerTarget = ref<DirectoryTarget>('SOURCE');
const selectedTreeNode = ref<TreeNodeData | null>(null);

const scanId = ref('');
const scanItems = ref<UnpackSourceScanItem[]>([]);
const scanLoading = ref(false);
const scanSelectedCount = ref(0);
const scanDiscoveredCount = ref(0);
const scanCursor = ref<string | undefined>();
const scanNextCursor = ref<string | null>(null);
const scanCursorHistory = ref<Array<string | undefined>>([]);
const scanKeyword = ref('');
const scanExtension = ref('');
const scanResolution = ref('');
const scanSelectedFilter = ref('');
const templatePreviewLoading = ref(false);
const templatePreview = ref<{
  discoveredCount: number | null;
  sourceCount: number;
  sourceName: string | null;
  sourceSizeBytes: number | null;
  sourceKind: SourceKind;
} | null>(null);

const executionVisible = ref(false);
const activeExecution = ref<UnpackExecution | null>(null);
const activeDefinition = ref<UnpackDefinition | null>(null);
const executionItems = ref<UnpackExecutionItem[]>([]);
const diagnosticsVisible = ref(false);
const diagnosticsLoading = ref(false);
const diagnosticsItemName = ref('');
const diagnostics = ref<UnpackItemDiagnostics | null>(null);
const detailTimeline = computed(() =>
  diagnostics.value ? unpackExecutionDetailTimeline(diagnostics.value) : [],
);
const detailSummary = computed(() =>
  diagnostics.value ? unpackExecutionDetailSummary(diagnostics.value) : null,
);

const diagnosticsGuard = createUnpackDiagnosticsGuard();
// Invalidate immediately on close, including requests that finish before the
// next Vue render. Opening another movie generates a new independent ticket.
watch(
  diagnosticsVisible,
  (visible) => {
    if (!visible) diagnosticsGuard.close();
  },
  { flush: 'sync' },
);
onBeforeUnmount(() => {
  diagnosticsGuard.close();
  unpackTableResizeObserver?.disconnect();
});
const executionLoading = ref(false);
const itemCursor = ref<string | undefined>();
const itemNextCursor = ref<string | null>(null);
const itemCursorHistory = ref<Array<string | undefined>>([]);
const itemPageSize = ref(20);
const itemPageIndex = ref(1);
const executionItemStatusFilter = ref('');
const executionItemKeyword = ref('');

const reviewVisible = ref(false);
const reviewLoading = ref(false);
const reviewSaving = ref(false);
const reviewItem = ref<UnpackExecutionItem | null>(null);
const reviewCandidates = ref<UnpackMatchCandidateList | null>(null);
const selectedCandidateId = ref('');
const manualCandidateVisible = ref(false);
const manualCandidatePreviewLoading = ref(false);
const manualCandidateSaving = ref(false);
const manualCandidatePreview = ref<ManualCandidatePreview | null>(null);
const manualSourceMode = ref<'URL' | 'FILE'>('URL');
const manualSiteId = ref('');
const manualSourceUrl = ref('');
const manualTorrentFile = ref<File | null>(null);
const manualSiteOptions = computed(() => {
  const allowedSiteIds = activeDefinition.value?.site_ids;
  return sites.value.filter(
    (site) => site.enabled && (!allowedSiteIds || allowedSiteIds.includes(site.id)),
  );
});

const draft = reactive<Draft>(freshDraft());

const createWizardSteps = computed(() =>
  draft.taskKind === 'TEMPLATE' ? templateWizardSteps : standardWizardSteps,
);
const currentWizardStep = computed(
  () => createWizardSteps.value[createStep.value - 1] ?? createWizardSteps.value[0],
);

const enabledSites = computed(() => sites.value.filter((item) => item.enabled));
const readyTemplates = computed(() =>
  templates.value.filter(
    (item) => item.status === 'READY' && item.site_keys.length === 1 && item.site_ids.length > 0,
  ),
);
const selectedTemplate = computed(
  () => templates.value.find((item) => item.id === draft.templateId) ?? null,
);
const selectedTemplateSites = computed(() =>
  enabledSites.value.filter((item) => (selectedTemplate.value?.site_ids ?? []).includes(item.id)),
);
const selectedTemplateBoundSiteLabel = computed(
  () =>
    selectedTemplateSites.value.find((item) => item.id === draft.siteIds[0])?.name ??
    selectedTemplate.value?.site_keys[0] ??
    '',
);
const selectedSourceTorrent = computed(
  () => sourceTorrents.value.find((item) => item.torrent_hash === draft.sourceTorrentHash) ?? null,
);
const enabledDownloaders = computed(() => downloaders.value.filter((item) => item.enabled));
const selectedSiteNames = computed(() =>
  enabledSites.value.filter((item) => draft.siteIds.includes(item.id)).map((item) => item.name),
);
const selectedSourceDownloader = computed(
  () => enabledDownloaders.value.find((item) => item.id === draft.sourceDownloaderId) ?? null,
);
const selectedTargetDownloader = computed(
  () => enabledDownloaders.value.find((item) => item.id === draft.targetDownloaderId) ?? null,
);
const storageModeLabel = computed(() => {
  if (draft.storageMode === 'SYMLINK') return '软链接';
  if (draft.storageMode === 'COPY') return '复制';
  return '硬链接';
});
const advancedRulesSummary = computed(() =>
  [
    draft.autoMatchEnabled ? '自动匹配开启' : '自动匹配关闭',
    draft.retryEnabled ? '自动重试开启' : '自动重试关闭',
    `重试次数 ${draft.maxRetries} 次`,
    ...(draft.autoMatchEnabled ? [`匹配阈值 ${draft.autoMatchPercent}%`] : []),
    `存放方式 ${storageModeLabel.value}`,
  ].join(' · '),
);
const targetCategoryOptions = ref<string[]>([]);
const targetTagOptions = ref<string[]>([]);
const targetDownloaderOptionsLoading = ref(false);
let targetDownloaderOptionsRequestId = 0;
let targetDownloaderOptionsLoadedFor = '';

const latestExecutionByDefinition = computed(() => {
  const result = new Map<string, UnpackExecution>();
  for (const item of executions.value) {
    const current = result.get(item.definition_id);
    if (!current || item.created_at > current.created_at) result.set(item.definition_id, item);
  }
  return result;
});

const rows = computed<TaskRow[]>(() =>
  definitions.value.map((definition) => ({
    definition,
    execution: latestExecutionByDefinition.value.get(definition.id) ?? null,
  })),
);

const visibleRows = computed(() => {
  const query = appliedKeyword.value.trim().toLocaleLowerCase();
  return rows.value.filter((row) => {
    if (appliedTypeFilter.value && row.definition.task_kind !== appliedTypeFilter.value)
      return false;
    const state = row.execution?.status ?? row.definition.status;
    if (appliedStatusFilter.value && state !== appliedStatusFilter.value) return false;
    if (!query) return true;
    return (
      row.definition.name.toLocaleLowerCase().includes(query) ||
      sourceText(row.definition).toLocaleLowerCase().includes(query)
    );
  });
});

function searchFilters(): void {
  appliedTypeFilter.value = typeFilter.value;
  appliedStatusFilter.value = statusFilter.value;
  appliedKeyword.value = keyword.value;
}

const selectedMediaCount = computed(() => scanSelectedCount.value);
const canSaveSelectedMedia = computed(() => selectedMediaCount.value > 0 && !saving.value);

const pipelineSteps = [
  '分页获取影片',
  '分批影片匹配',
  '自动匹配 / 人工审核',
  '种子内容校验',
  '辅种执行',
  '客户端校验 / 完成',
];

const activePipelineIndex = computed(() => {
  const status = activeExecution.value?.status;
  if (!status) return 0;
  if (status === 'DISCOVERING') return 0;
  if (status === 'MATCHING') return 1;
  if (status === 'REVIEW_REQUIRED') return 2;
  if (status === 'CONTENT_VERIFYING') return 3;
  if (status === 'EXECUTING') return 4;
  if (status === 'CLIENT_VERIFYING') return 5;
  return 5;
});

function freshDraft(): Draft {
  return {
    name: '',
    triggerKind: 'MANUAL',
    taskKind: 'MANUAL',
    templateId: '',
    templateVersion: null,
    sourceKind: 'DIRECTORY',
    sourceTorrentHash: '',
    siteIds: [],
    sourceDirectory: '',
    sourceToken: '',
    sourceDownloaderId: '',
    downloaderNameContains: '',
    downloaderCategories: '',
    downloaderTags: '',
    outputDirectory: '',
    targetDownloaderId: '',
    targetCategory: '',
    targetTags: [],
    extensions: '.mkv, .mp4, .ts, .m2ts',
    minSizeMb: 0,
    maxSizeMb: null,
    includeName: '',
    excludeNames:
      'sample, trailer, SP., featurette, behind the scenes, making of, deleted scene, short film, bonus feature, interview',
    includeSubdirectories: true,
    storageMode: 'HARDLINK',
    retryEnabled: true,
    maxRetries: 3,
    autoMatchEnabled: true,
    autoMatchPercent: 99,
    cronExpression: '*/10 * * * *',
  };
}

function clearSourceTorrentList(): void {
  sourceTorrentsRequestId += 1;
  sourceTorrentsLoadedFor = '';
  sourceTorrentsLoadingFor = '';
  sourceTorrentsLoading.value = false;
  sourceTorrents.value = [];
}

function resetDraft(): void {
  Object.assign(draft, freshDraft());
  clearSourceTorrentList();
  templatePreview.value = null;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : {};
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : [];
}

function sourceText(definition: UnpackDefinition): string {
  const source = asRecord(definition.source_config);
  if (definition.source_kind === 'DIRECTORY')
    return stringValue(source.directory_path) || '目录未配置';
  const downloaderId = stringValue(source.downloader_id);
  const downloader = downloaders.value.find((item) => item.id === downloaderId);
  const extras = [
    stringValue(source.name_contains),
    ...stringList(source.categories),
    ...stringList(source.tags),
  ].filter(Boolean);
  return [downloader?.name ?? '下载器已删除', ...extras].join(' · ');
}

function siteText(definition: UnpackDefinition): string {
  return definition.site_ids
    .map((id) => sites.value.find((site) => site.id === id)?.name ?? '站点已删除')
    .join('、');
}

function taskStatusText(row: TaskRow): string {
  return row.execution
    ? unpackExecutionStatusLabel(row.execution.status)
    : unpackDefinitionStatusLabel(row.definition.status);
}

function statusTag(row: TaskRow): 'primary' | 'success' | 'warning' | 'danger' | 'info' {
  const status = row.execution?.status ?? row.definition.status;
  if (status === 'COMPLETED' || status === 'ENABLED') return 'success';
  if (status === 'REVIEW_REQUIRED' || status === 'PAUSED') return 'warning';
  if (['COMPLETED_WITH_ERRORS', 'FAILED', 'ERROR'].includes(status)) return 'danger';
  if (status === 'PENDING_EXECUTION') return 'info';
  return 'primary';
}

function itemStatusTag(
  status: UnpackItemStatus,
): 'primary' | 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'COMPLETED' || status === 'CONTENT_VERIFIED') return 'success';
  if (status === 'REVIEW_REQUIRED') return 'warning';
  if (
    ['NO_MATCH', 'MATCH_TIMEOUT', 'MATCH_ERROR', 'CONTENT_MISMATCH', 'EXECUTION_ERROR'].includes(
      status,
    )
  ) {
    return 'danger';
  }
  if (status === 'DISCOVERED' || status === 'MATCH_PENDING') return 'info';
  return 'primary';
}

function resultText(row: TaskRow): string {
  const execution = row.execution;
  if (!execution) {
    return row.definition.execution_scope_kind === 'SELECTED_MEDIA'
      ? '指定 ' + row.definition.selected_source_count + ' 个影片'
      : '全部影视文件';
  }
  return (
    '无匹配 ' +
    execution.no_match_count +
    ' · 自动 ' +
    execution.matched_auto_count +
    ' · 待审核 ' +
    execution.review_count +
    ' · 通过 ' +
    execution.content_verified_count +
    ' · 完成 ' +
    execution.completed_count +
    ' · 异常 ' +
    (execution.timeout_count + execution.error_count + execution.content_mismatch_count)
  );
}

function progressCount(execution: UnpackExecution): number {
  return unpackProgressCount(execution.total_count, {
    matchedAutoCount: execution.matched_auto_count,
    noMatchCount: execution.no_match_count,
    reviewCount: execution.review_count,
    contentVerifiedCount: execution.content_verified_count,
    contentMismatchCount: execution.content_mismatch_count,
    timeoutCount: execution.timeout_count,
    errorCount: execution.error_count,
    completedCount: execution.completed_count,
  });
}

function progressText(execution: UnpackExecution | null): string {
  if (!execution || execution.total_count <= 0) return '—';
  return progressCount(execution) + ' / ' + execution.total_count;
}

function progressPercent(execution: UnpackExecution | null): number {
  if (!execution) return 0;
  return unpackProgressPercent(execution.total_count, progressCount(execution));
}

function formatTime(value: string | null | undefined): string {
  if (!value) return '—';
  return new Date(value).toLocaleString('zh-CN', { hour12: false });
}

function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return amount.toFixed(index >= 3 ? 2 : 1) + ' ' + units[index];
}

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    const [nextDefinitions, nextExecutions, nextSites, nextDownloaders, nextTemplates] =
      await Promise.all([
        listUnpackDefinitions(),
        listUnpackExecutions({ limit: 500 }),
        listSites(),
        listDownloaders(),
        listUnpackTemplates(),
      ]);
    definitions.value = nextDefinitions;
    executions.value = nextExecutions;
    sites.value = nextSites;
    downloaders.value = nextDownloaders;
    templates.value = nextTemplates;
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    loading.value = false;
  }
}

function openCreate(): void {
  editingDefinition.value = null;
  resetDraft();
  createStep.value = 1;
  advancedRulesExpanded.value = false;
  targetDownloaderOptionsRequestId += 1;
  targetDownloaderOptionsLoading.value = false;
  targetDownloaderOptionsLoadedFor = '';
  targetCategoryOptions.value = [];
  targetTagOptions.value = [];
  createVisible.value = true;
}

function openEdit(row: TaskRow): void {
  const definition = row.definition;
  editingDefinition.value = definition;
  resetDraft();
  createStep.value = 1;
  advancedRulesExpanded.value = false;
  const source = asRecord(definition.source_config);
  const filter = asRecord(definition.file_filter);
  const output = asRecord(definition.output_config);
  Object.assign(draft, {
    name: definition.name,
    triggerKind: definition.trigger_kind,
    taskKind: definition.task_kind,
    templateId: definition.template_id ?? '',
    templateVersion: definition.template_version,
    sourceKind: definition.source_kind,
    sourceTorrentHash: stringValue(source.torrent_hash),
    siteIds: [...definition.site_ids],
    sourceDirectory: stringValue(source.directory_path),
    sourceDownloaderId: stringValue(source.downloader_id),
    downloaderNameContains: stringValue(source.name_contains),
    downloaderCategories: stringList(source.categories).join(', '),
    downloaderTags: stringList(source.tags).join(', '),
    outputDirectory: stringValue(output.output_directory),
    targetDownloaderId: stringValue(output.target_downloader_id),
    targetCategory: stringValue(output.target_category),
    targetTags: stringList(output.target_tags),
    extensions: stringList(filter.extensions).join(', '),
    minSizeMb: typeof filter.min_size_bytes === 'number' ? filter.min_size_bytes / 1048576 : null,
    maxSizeMb: typeof filter.max_size_bytes === 'number' ? filter.max_size_bytes / 1048576 : null,
    includeName: stringValue(filter.include_name),
    excludeNames: stringList(filter.exclude_names).join(', '),
    includeSubdirectories: filter.include_subdirectories !== false,
    storageMode:
      output.storage_mode === 'SYMLINK' || output.storage_mode === 'COPY'
        ? output.storage_mode
        : 'HARDLINK',
    retryEnabled: definition.retry_enabled,
    maxRetries: definition.max_retries,
    autoMatchEnabled: definition.auto_match_enabled,
    autoMatchPercent: definition.auto_match_threshold_bps / 100,
    cronExpression: definition.cron_expression ?? '*/10 * * * *',
  });
  if (definition.cron_expression) applyCronPreset(definition.cron_expression);
  targetDownloaderOptionsRequestId += 1;
  targetDownloaderOptionsLoading.value = false;
  targetDownloaderOptionsLoadedFor = '';
  targetCategoryOptions.value = draft.targetCategory ? [draft.targetCategory] : [];
  targetTagOptions.value = [...draft.targetTags];
  createVisible.value = true;
  if (definition.task_kind === 'TEMPLATE' && definition.source_kind === 'DOWNLOADER') {
    void loadSourceTorrents(draft.sourceDownloaderId);
  }
}

function definitionCanDelete(_row: TaskRow): boolean {
  // A list aggregate cannot prove the absence of matching-state items.
  // Always expose Delete and let the atomic backend query reject only
  // MATCH_PENDING / MATCHING, including in older execution runs.
  return true;
}

function definitionDeleteDisabledReason(_row: TaskRow): string {
  return '仅当任务没有待匹配或匹配中的影片时可以删除；服务端会再次核对';
}

async function deleteDefinition(row: TaskRow): Promise<void> {
  if (!definitionCanDelete(row)) {
    ElMessage.warning(definitionDeleteDisabledReason(row));
    return;
  }
  try {
    await ElMessageBox.confirm(
      '确认删除“' +
        row.definition.name +
        '”？只要没有待匹配或匹配中的影片即可删除。此操作只清理 PackBreaker 任务记录，不删除影片、辅助文件或下载器种子；已执行的外部操作会独立留存历史证据。',
      '删除数据拆包任务',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
    );
  } catch (caught) {
    if (caught === 'cancel' || caught === 'close') return;
    throw caught;
  }
  try {
    await deleteUnpackDefinition(row.definition.id, row.definition.version);
    ElMessage.success('任务已删除');
    await refresh();
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  }
}

const retryingDefinitionId = ref<string | null>(null);
const retryDialogVisible = ref(false);
const retryTargetRow = ref<TaskRow | null>(null);
const selectedRetryStatuses = ref<UnpackItemStatus[]>([]);
const retryStatusOptions: Array<{ value: UnpackItemStatus; label: string }> = [
  { value: 'NO_MATCH', label: '无匹配' },
  { value: 'REVIEW_REQUIRED', label: '待人工审核' },
  { value: 'MATCH_TIMEOUT', label: '匹配超时' },
  { value: 'MATCH_ERROR', label: '匹配错误' },
  { value: 'CONTENT_MISMATCH', label: '内容不匹配' },
  { value: 'EXECUTION_ERROR', label: '执行异常' },
  { value: 'MATCHED_AUTO', label: '自动匹配成功' },
  { value: 'MATCHED_MANUAL', label: '人工匹配成功' },
];

function openRetryDialog(row: TaskRow): void {
  if (!row.execution || retryingDefinitionId.value) return;
  retryTargetRow.value = row;
  selectedRetryStatuses.value = [];
  retryDialogVisible.value = true;
}

async function retryDefinition(): Promise<void> {
  const row = retryTargetRow.value;
  const execution = row?.execution;
  if (!row || !execution || !selectedRetryStatuses.value.length || retryingDefinitionId.value)
    return;
  retryingDefinitionId.value = row.definition.id;
  try {
    const result = await retryUnpackExecutionItems(
      execution.id,
      execution.version,
      selectedRetryStatuses.value,
    );
    retryDialogVisible.value = false;
    if (result.retried_count > 0) {
      ElMessage.success(
        result.retried_count +
          ' 个影片已重新加入匹配队列，' +
          result.skipped_unsafe_count +
          ' 个影片存在文件或下载器操作记录，已安全跳过，请逐项核对后处理',
      );
    } else {
      ElMessage.info('没有可重新匹配的影片');
    }
    await refresh();
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
    await refresh();
  } finally {
    retryingDefinitionId.value = null;
  }
}

function showExecuteAction(row: TaskRow): boolean {
  return !row.execution && row.definition.status === 'PENDING_EXECUTION';
}

function showViewAction(row: TaskRow): boolean {
  return row.execution !== null;
}

function showRetryAction(row: TaskRow): boolean {
  return row.execution !== null && row.execution.total_count > 0;
}

function handleDefinitionMenu(command: string, row: TaskRow): void {
  if (command === 'edit') {
    openEdit(row);
  } else if (command === 'delete') {
    void deleteDefinition(row);
  }
}

function chooseTaskKind(kind: TaskKind): void {
  clearSourceTorrentList();
  draft.taskKind = kind;
  draft.triggerKind = kind === 'MONITOR' ? 'MONITOR' : 'MANUAL';
  draft.cronExpression = kind === 'MONITOR' ? draft.cronExpression || '*/10 * * * *' : '';
  if (kind !== 'TEMPLATE') {
    draft.templateId = '';
    draft.templateVersion = null;
    draft.sourceKind = 'DIRECTORY';
    draft.sourceTorrentHash = '';
    draft.siteIds = [];
    templatePreview.value = null;
  } else {
    draft.sourceKind = 'DIRECTORY';
    const firstSiteId = selectedTemplate.value?.site_ids[0];
    draft.siteIds = firstSiteId ? [firstSiteId] : [];
  }
}

function onTemplateChange(templateId: string): void {
  const template = templates.value.find((item) => item.id === templateId);
  draft.templateVersion = template?.current_version ?? null;
  const firstSiteId = template?.status === 'READY' ? template.site_ids[0] : undefined;
  draft.siteIds = firstSiteId ? [firstSiteId] : [];
  templatePreview.value = null;
}

function onTemplateSiteChange(siteId: string): void {
  const allowed = selectedTemplate.value?.site_ids ?? [];
  draft.siteIds = siteId && allowed.includes(siteId) ? [siteId] : [];
  templatePreview.value = null;
}

function templateSiteLabel(template: UnpackTemplate): string {
  const site = enabledSites.value.find((item) => template.site_ids.includes(item.id));
  return site?.name ?? template.site_keys[0] ?? '未绑定站点';
}

function templateMatchingStatusLabel(template: UnpackTemplate): string {
  const status = template.source_provenance.matching_status;
  if (status === 'COMPLETED') return '匹配完成';
  if (status !== 'PARTIAL') return '';
  const rawEvidence = template.source_provenance.matching_evidence;
  if (typeof rawEvidence === 'object' && rawEvidence !== null && !Array.isArray(rawEvidence)) {
    const evidence = rawEvidence as Record<string, unknown>;
    const matched = evidence.matched_movie_directory_count;
    const expected = evidence.expected_movie_directory_count;
    if (typeof matched === 'number' && typeof expected === 'number') {
      return `部分匹配 ${matched}/${expected}`;
    }
  }
  return '部分匹配';
}

function templateOptionLabel(template: UnpackTemplate): string {
  const parts = [
    template.name,
    templateSiteLabel(template),
    `v${template.current_version}`,
    `${template.seed_count} 个种子`,
    `${template.mapped_media_count} 个目录映射`,
  ];
  const matchingStatus = templateMatchingStatusLabel(template);
  if (matchingStatus) parts.push(matchingStatus);
  return parts.join(' · ');
}

function chooseSource(kind: SourceKind): void {
  if (draft.taskKind !== 'TEMPLATE' && draft.triggerKind === 'MANUAL' && kind === 'DOWNLOADER')
    return;
  draft.sourceKind = kind;
  templatePreview.value = null;
  if (kind === 'DIRECTORY') {
    draft.sourceTorrentHash = '';
    clearSourceTorrentList();
  }
  if (kind === 'DOWNLOADER' && draft.sourceDownloaderId && !draft.targetDownloaderId) {
    draft.targetDownloaderId = draft.sourceDownloaderId;
    onTargetDownloaderChange(draft.targetDownloaderId);
  }
}

function onSourceDownloaderChange(value: string): void {
  clearSourceTorrentList();
  draft.sourceTorrentHash = '';
  templatePreview.value = null;
  if (draft.sourceKind === 'DOWNLOADER' && !draft.targetDownloaderId) {
    draft.targetDownloaderId = value;
    onTargetDownloaderChange(value);
  }
}

function onSourceTorrentChange(value: string): void {
  draft.sourceTorrentHash = value;
  const torrent = sourceTorrents.value.find((item) => item.torrent_hash === value);
  draft.downloaderNameContains = torrent?.name ?? '';
  templatePreview.value = null;
}

async function loadSourceTorrents(downloaderId: string): Promise<void> {
  if (!downloaderId) return;
  if (sourceTorrentsLoadedFor === downloaderId) return;
  if (sourceTorrentsLoading.value && sourceTorrentsLoadingFor === downloaderId) return;

  const requestId = ++sourceTorrentsRequestId;
  sourceTorrentsLoading.value = true;
  sourceTorrentsLoadingFor = downloaderId;
  try {
    const collected: DownloaderTorrent[] = [];
    let total = Number.POSITIVE_INFINITY;
    for (let pageNumber = 1; pageNumber <= 10 && collected.length < total; pageNumber += 1) {
      const page = await listDownloaderTorrents(downloaderId, { page: pageNumber, page_size: 200 });
      if (requestId !== sourceTorrentsRequestId) return;
      collected.push(...page.items);
      total = page.total;
      if (!page.items.length) break;
    }
    if (requestId !== sourceTorrentsRequestId) return;
    sourceTorrents.value = collected
      .filter((item) => item.progress >= 1)
      .sort((left, right) => left.name.localeCompare(right.name));
    sourceTorrentsLoadedFor = downloaderId;
    if (!sourceTorrents.value.length) ElMessage.info('该下载器暂时没有可选择的已完成种子');
  } catch (caught) {
    if (requestId !== sourceTorrentsRequestId) return;
    ElMessage.warning(`无法读取下载器种子列表：${toApiProblem(caught).message}`);
  } finally {
    if (requestId === sourceTorrentsRequestId) {
      sourceTorrentsLoading.value = false;
      sourceTorrentsLoadingFor = '';
    }
  }
}

function onTargetDownloaderChange(value: string): void {
  targetDownloaderOptionsRequestId += 1;
  targetDownloaderOptionsLoading.value = false;
  targetDownloaderOptionsLoadedFor = '';
  targetCategoryOptions.value = draft.targetCategory ? [draft.targetCategory] : [];
  targetTagOptions.value = [...draft.targetTags];
  if (!value) return;
}

async function loadTargetDownloaderOptions(downloaderId: string, force = false): Promise<void> {
  if (!downloaderId) {
    targetCategoryOptions.value = draft.targetCategory ? [draft.targetCategory] : [];
    targetTagOptions.value = [...draft.targetTags];
    targetDownloaderOptionsLoadedFor = '';
    return;
  }
  if (!force && targetDownloaderOptionsLoadedFor === downloaderId) return;
  const requestId = ++targetDownloaderOptionsRequestId;
  targetDownloaderOptionsLoading.value = true;
  try {
    const page = await listDownloaderTorrents(downloaderId, { page: 1, page_size: 200 });
    if (requestId !== targetDownloaderOptionsRequestId) return;
    targetCategoryOptions.value = [
      ...new Set(
        page.items.map((item) => item.category).filter((value): value is string => Boolean(value)),
      ),
    ].sort();
    targetTagOptions.value = [...new Set(page.items.flatMap((item) => item.tags))].sort();
    targetDownloaderOptionsLoadedFor = downloaderId;
  } catch {
    if (requestId !== targetDownloaderOptionsRequestId) return;
    targetCategoryOptions.value = draft.targetCategory ? [draft.targetCategory] : [];
    targetTagOptions.value = [...draft.targetTags];
    targetDownloaderOptionsLoadedFor = '';
    ElMessage.warning('无法读取目标下载器现有分类和标签，可直接输入新值');
  } finally {
    if (requestId === targetDownloaderOptionsRequestId)
      targetDownloaderOptionsLoading.value = false;
  }
}

async function prepareTemplateOverview(): Promise<boolean> {
  templatePreviewLoading.value = true;
  try {
    if (draft.sourceKind === 'DIRECTORY') {
      if (!draft.sourceToken) {
        if (editingDefinition.value) {
          templatePreview.value = {
            discoveredCount: null,
            sourceCount: 1,
            sourceName: draft.sourceDirectory,
            sourceSizeBytes: null,
            sourceKind: 'DIRECTORY',
          };
          return true;
        }
        ElMessage.warning('请通过目录选择器重新选择来源目录');
        return false;
      }
      const scan = await createUnpackSourceScan({
        selection_token: draft.sourceToken,
        file_filter: fileFilterPayload(),
      });
      templatePreview.value = {
        discoveredCount: scan.discovered_count,
        sourceCount: 1,
        sourceName: draft.sourceDirectory,
        sourceSizeBytes: null,
        sourceKind: 'DIRECTORY',
      };
      return true;
    }
    const torrent = selectedSourceTorrent.value;
    if (!torrent) {
      ElMessage.warning('请选择一个已完成的来源种子');
      return false;
    }
    templatePreview.value = {
      discoveredCount: null,
      sourceCount: 1,
      sourceName: torrent.name,
      sourceSizeBytes: torrent.size_bytes,
      sourceKind: 'DOWNLOADER',
    };
    return true;
  } catch (caught) {
    ElMessage.error(`来源扫描失败：${toApiProblem(caught).message}`);
    return false;
  } finally {
    templatePreviewLoading.value = false;
  }
}

async function nextWizardStep(): Promise<void> {
  if (!validateWizardStep(createStep.value)) return;
  if (draft.taskKind === 'TEMPLATE' && createStep.value === 2) {
    if (!(await prepareTemplateOverview())) return;
  }
  createStep.value = Math.min(createWizardSteps.value.length, createStep.value + 1);
}

function previousWizardStep(): void {
  createStep.value = Math.max(1, createStep.value - 1);
}

function goToWizardStep(step: number): void {
  if (step >= 1 && step < createStep.value && step <= createWizardSteps.value.length) {
    createStep.value = step;
  }
}
function applyCronPreset(value: string): void {
  draft.cronExpression = value;
}

function splitCsv(value: string): string[] {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

function fileFilterPayload(): Record<string, unknown> {
  const extensions = splitCsv(draft.extensions);
  return {
    extensions,
    min_size_bytes:
      draft.minSizeMb === null ? null : Math.max(0, Math.round(draft.minSizeMb * 1024 * 1024)),
    max_size_bytes:
      draft.maxSizeMb === null ? null : Math.max(0, Math.round(draft.maxSizeMb * 1024 * 1024)),
    include_name: draft.includeName.trim() || null,
    exclude_names: splitCsv(draft.excludeNames),
    include_subdirectories: draft.includeSubdirectories,
  };
}

function validateDraft(): boolean {
  if (!draft.name.trim()) {
    ElMessage.warning('请输入任务名称');
    return false;
  }
  if (!draft.siteIds.length || (draft.taskKind === 'TEMPLATE' && draft.siteIds.length !== 1)) {
    ElMessage.warning(
      draft.taskKind === 'TEMPLATE' ? '请选择模板绑定的一个站点配置' : '至少选择一个扫描站点',
    );
    return false;
  }
  if (draft.taskKind === 'TEMPLATE' && (!draft.templateId || !draft.templateVersion)) {
    ElMessage.warning('请选择已完成入库的拆包模板');
    return false;
  }
  if (!splitCsv(draft.extensions).length) {
    ElMessage.warning('至少保留一个影视文件后缀');
    return false;
  }
  if (!draft.outputDirectory.trim()) {
    ElMessage.warning('请选择输出目录');
    return false;
  }
  if (!draft.targetDownloaderId) {
    ElMessage.warning('请选择目标下载器');
    return false;
  }
  if (draft.sourceKind === 'DIRECTORY' && !draft.sourceDirectory.trim()) {
    ElMessage.warning('请选择来源目录');
    return false;
  }
  if (draft.sourceKind === 'DOWNLOADER' && !draft.sourceDownloaderId) {
    ElMessage.warning('请选择来源下载器');
    return false;
  }
  if (
    draft.taskKind === 'TEMPLATE' &&
    draft.sourceKind === 'DOWNLOADER' &&
    !draft.sourceTorrentHash
  ) {
    ElMessage.warning('请选择一个已完成的来源种子');
    return false;
  }
  if (draft.triggerKind === 'MONITOR' && !draft.cronExpression.trim()) {
    ElMessage.warning('请输入或通过 Cron 辅助生成执行时间');
    return false;
  }
  return true;
}
function validateWizardStep(step: number): boolean {
  if (step === 1) {
    if (!draft.name.trim()) {
      ElMessage.warning('请输入任务名称');
      return false;
    }
    if (draft.taskKind === 'TEMPLATE') {
      if (
        !draft.templateId ||
        !draft.templateVersion ||
        selectedTemplate.value?.status !== 'READY'
      ) {
        ElMessage.warning('请选择已完成入库的拆包模板');
        return false;
      }
      if (
        draft.siteIds.length !== 1 ||
        !selectedTemplate.value?.site_ids.includes(draft.siteIds[0])
      ) {
        ElMessage.warning('请选择模板绑定的一个站点配置');
        return false;
      }
    } else if (!draft.siteIds.length) {
      ElMessage.warning('至少选择一个扫描站点');
      return false;
    }
    if (draft.triggerKind === 'MONITOR' && !draft.cronExpression.trim()) {
      ElMessage.warning('请输入监控任务的 Cron 执行时间');
      return false;
    }
  }
  if (step === 2) {
    if (draft.sourceKind === 'DIRECTORY' && !draft.sourceDirectory.trim()) {
      ElMessage.warning('请选择来源目录');
      return false;
    }
    if (draft.sourceKind === 'DOWNLOADER' && !draft.sourceDownloaderId) {
      ElMessage.warning('请选择来源下载器');
      return false;
    }
    if (
      draft.taskKind === 'TEMPLATE' &&
      draft.sourceKind === 'DOWNLOADER' &&
      !draft.sourceTorrentHash
    ) {
      ElMessage.warning('请选择一个已完成的来源种子');
      return false;
    }
    if (!draft.outputDirectory.trim()) {
      ElMessage.warning('请选择输出目录');
      return false;
    }
    if (!draft.targetDownloaderId) {
      ElMessage.warning('请选择目标下载器');
      return false;
    }
  }
  if (step === 3 && draft.taskKind === 'TEMPLATE') return true;
  if (step === 3) {
    if (!splitCsv(draft.extensions).length) {
      ElMessage.warning('至少保留一个影视文件后缀');
      return false;
    }
    if (draft.minSizeMb !== null && draft.maxSizeMb !== null && draft.maxSizeMb < draft.minSizeMb) {
      ElMessage.warning('最大文件大小不能小于最小文件大小');
      return false;
    }
  }
  return true;
}

function toggleAdvancedRules(): void {
  advancedRulesExpanded.value = !advancedRulesExpanded.value;
}

function createPayload(
  scope: 'ALL_MATCHING_MEDIA' | 'SELECTED_MEDIA',
  selectedScanId?: string,
): UnpackDefinitionCreate {
  const sourceConfig: Record<string, unknown> =
    draft.sourceKind === 'DIRECTORY'
      ? { directory_path: draft.sourceDirectory.trim() }
      : {
          downloader_id: draft.sourceDownloaderId,
          name_contains: draft.downloaderNameContains.trim() || null,
          categories: splitCsv(draft.downloaderCategories),
          tags: splitCsv(draft.downloaderTags),
          torrent_hash: draft.taskKind === 'TEMPLATE' ? draft.sourceTorrentHash || null : null,
        };
  return {
    name: draft.name.trim(),
    trigger_kind: draft.triggerKind,
    task_kind: draft.taskKind,
    template_id: draft.taskKind === 'TEMPLATE' ? draft.templateId : null,
    template_version: draft.taskKind === 'TEMPLATE' ? draft.templateVersion : null,
    source_kind: draft.sourceKind,
    execution_scope_kind: scope,
    source_config: sourceConfig,
    file_filter: fileFilterPayload(),
    site_ids: [...draft.siteIds],
    output_config: {
      output_directory: draft.outputDirectory.trim(),
      storage_mode: draft.storageMode,
      conflict_policy: 'VERIFY_REUSE_OR_STOP',
      target_downloader_id: draft.targetDownloaderId,
      target_category: draft.targetCategory.trim() || null,
      target_tags: [...new Set(draft.targetTags.map((tag) => tag.trim()).filter(Boolean))],
    },
    retry_enabled: draft.retryEnabled,
    max_retries: draft.maxRetries,
    auto_match_threshold_bps: Math.round(Math.min(100, Math.max(0, draft.autoMatchPercent)) * 100),
    auto_match_enabled: draft.autoMatchEnabled,
    cron_expression: draft.triggerKind === 'MONITOR' ? draft.cronExpression.trim() : null,
    timezone: draft.triggerKind === 'MONITOR' ? (editingDefinition.value?.timezone ?? null) : null,
    source_scan_id: selectedScanId ?? null,
  };
}

async function saveDraft(): Promise<void> {
  if (!validateDraft()) return;
  if (editingDefinition.value) {
    await persistDefinition(editingDefinition.value.execution_scope_kind);
    return;
  }
  if (
    draft.taskKind !== 'TEMPLATE' &&
    draft.triggerKind === 'MANUAL' &&
    draft.sourceKind === 'DIRECTORY'
  ) {
    createVisible.value = false;
    scopeConfirmVisible.value = true;
    return;
  }
  await persistDefinition('ALL_MATCHING_MEDIA');
}

async function persistDefinition(
  scope: 'ALL_MATCHING_MEDIA' | 'SELECTED_MEDIA',
  selectedScanId?: string,
): Promise<void> {
  saving.value = true;
  try {
    if (editingDefinition.value) {
      await updateUnpackDefinition(
        editingDefinition.value.id,
        editingDefinition.value.version,
        createPayload(scope, selectedScanId),
      );
    } else {
      await createUnpackDefinition(createPayload(scope, selectedScanId));
    }
    createVisible.value = false;
    scopeConfirmVisible.value = false;
    mediaSelectVisible.value = false;
    ElMessage.success(
      editingDefinition.value ? '任务配置已更新' : '任务已保存，当前状态为“待执行”',
    );
    editingDefinition.value = null;
    await refresh();
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    saving.value = false;
  }
}

async function useAllMedia(): Promise<void> {
  await persistDefinition('ALL_MATCHING_MEDIA');
}

async function chooseMediaFiles(): Promise<void> {
  if (!draft.sourceToken) {
    ElMessage.warning('选择影片前，请使用目录树重新选择来源目录');
    scopeConfirmVisible.value = false;
    createVisible.value = true;
    return;
  }
  scopeConfirmVisible.value = false;
  scanLoading.value = true;
  try {
    const scan = await createUnpackSourceScan({
      selection_token: draft.sourceToken,
      file_filter: fileFilterPayload(),
    });
    scanId.value = scan.id;
    scanDiscoveredCount.value = scan.discovered_count;
    scanSelectedCount.value = scan.selected_count;
    scanKeyword.value = '';
    scanExtension.value = '';
    scanResolution.value = '';
    scanSelectedFilter.value = '';
    scanCursorHistory.value = [];
    await loadScanItems();
    mediaSelectVisible.value = true;
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
    createVisible.value = true;
  } finally {
    scanLoading.value = false;
  }
}

async function loadScanItems(cursor?: string): Promise<void> {
  if (!scanId.value) return;
  scanLoading.value = true;
  try {
    const result = await listUnpackSourceScanItems(scanId.value, {
      cursor,
      limit: 50,
      q: scanKeyword.value.trim() || undefined,
      extension: scanExtension.value || undefined,
      resolution: scanResolution.value || undefined,
      selected:
        scanSelectedFilter.value === 'selected'
          ? true
          : scanSelectedFilter.value === 'unselected'
            ? false
            : undefined,
    });
    scanCursor.value = cursor;
    scanItems.value = result.items;
    scanNextCursor.value = result.next_cursor;
    const summary = await getUnpackSourceScanSelectionSummary(scanId.value);
    scanSelectedCount.value = summary.selected_count;
    scanDiscoveredCount.value = summary.discovered_count;
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    scanLoading.value = false;
  }
}

async function toggleScanItem(item: UnpackSourceScanItem, value: unknown): Promise<void> {
  const selected = value === true;
  try {
    const summary = await updateUnpackSourceScanSelection(scanId.value, {
      source_object_keys: [item.source_object_key],
      selected,
    });
    item.selected = selected;
    scanSelectedCount.value = summary.selected_count;
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
    await loadScanItems(scanCursor.value);
  }
}

async function setVisibleSelection(selected: boolean): Promise<void> {
  if (!scanItems.value.length) return;
  try {
    const summary = await updateUnpackSourceScanSelection(scanId.value, {
      source_object_keys: scanItems.value.map((item) => item.source_object_key),
      selected,
    });
    scanItems.value = scanItems.value.map((item) => ({ ...item, selected }));
    scanSelectedCount.value = summary.selected_count;
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  }
}

async function nextScanPage(): Promise<void> {
  if (!scanNextCursor.value) return;
  scanCursorHistory.value.push(scanCursor.value);
  await loadScanItems(scanNextCursor.value);
}

async function previousScanPage(): Promise<void> {
  if (!scanCursorHistory.value.length) return;
  const previous = scanCursorHistory.value.pop();
  await loadScanItems(previous);
}

async function saveSelectedMedia(): Promise<void> {
  if (!canSaveSelectedMedia.value) return;
  await persistDefinition('SELECTED_MEDIA', scanId.value);
}

function openDirectoryPicker(target: DirectoryTarget): void {
  directoryPickerTarget.value = target;
  selectedTreeNode.value = null;
  directoryPickerVisible.value = true;
}

async function loadTreeNode(
  node: { level: number; data?: TreeNodeData },
  resolve: (data: TreeNodeData[]) => void,
): Promise<void> {
  try {
    const entries: UnpackTreeEntry[] =
      node.level === 0
        ? await listUnpackTreeRoots()
        : await browseUnpackTree(node.data?.token ?? '').then((result) => result.entries);
    resolve(
      entries.map((entry) => ({
        name: entry.name,
        path: entry.display_path,
        token: entry.selection_token,
      })),
    );
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
    resolve([]);
  }
}

function onTreeNodeClick(data: TreeNodeData): void {
  selectedTreeNode.value = data;
}

function confirmDirectorySelection(): void {
  const selected = selectedTreeNode.value;
  if (!selected) {
    ElMessage.warning('请选择一个目录');
    return;
  }
  if (directoryPickerTarget.value === 'SOURCE') {
    draft.sourceDirectory = selected.path;
    draft.sourceToken = selected.token;
    templatePreview.value = null;
  } else {
    draft.outputDirectory = selected.path;
  }
  directoryPickerVisible.value = false;
}

async function runDefinition(row: TaskRow): Promise<void> {
  try {
    const result = await runUnpackDefinition(row.definition.id);
    if (result.execution_id) ElMessage.success('任务已进入执行队列');
    else ElMessage.success('监控任务已启用并等待 Cron 调度');
    await refresh();
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  }
}

async function openExecution(row: TaskRow): Promise<void> {
  const execution = row.execution;
  if (!execution) {
    ElMessage.info('该任务尚未产生执行记录');
    return;
  }
  activeDefinition.value = row.definition;
  activeExecution.value = await getUnpackExecution(execution.id);
  itemCursorHistory.value = [];
  itemPageSize.value = 20;
  itemPageIndex.value = 1;
  executionItemStatusFilter.value = '';
  executionItemKeyword.value = '';
  await loadExecutionItems();
  executionVisible.value = true;
}

async function loadExecutionItems(cursor?: string): Promise<void> {
  if (!activeExecution.value) return;
  executionLoading.value = true;
  try {
    const result = await listUnpackExecutionItems(activeExecution.value.id, {
      cursor,
      limit: itemPageSize.value,
      status: (executionItemStatusFilter.value || undefined) as UnpackItemStatus | undefined,
      q: executionItemKeyword.value.trim() || undefined,
    });
    itemCursor.value = cursor;
    executionItems.value = result.items;
    itemNextCursor.value = result.next_cursor;
    activeExecution.value = await getUnpackExecution(activeExecution.value.id);
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    executionLoading.value = false;
  }
}

async function nextItemPage(): Promise<void> {
  if (!itemNextCursor.value) return;
  itemCursorHistory.value.push(itemCursor.value);
  await loadExecutionItems(itemNextCursor.value);
  itemPageIndex.value += 1;
}

async function previousItemPage(): Promise<void> {
  if (!itemCursorHistory.value.length) return;
  const previous = itemCursorHistory.value.pop();
  await loadExecutionItems(previous);
  itemPageIndex.value -= 1;
}

async function changeItemPageSize(value: number): Promise<void> {
  itemPageSize.value = value;
  itemPageIndex.value = 1;
  itemCursorHistory.value = [];
  await loadExecutionItems();
}

async function applyExecutionItemFilters(): Promise<void> {
  itemPageIndex.value = 1;
  itemCursorHistory.value = [];
  await loadExecutionItems();
}

async function refreshExecutionItems(): Promise<void> {
  await loadExecutionItems(itemCursor.value);
}

function mediaTitle(item: UnpackExecutionItem): string {
  const identity = asRecord(item.media_identity);
  const rawName = stringValue(identity.raw_name);
  if (rawName) return rawName;
  const snapshot = asRecord(item.source_snapshot);
  return (
    stringValue(snapshot.relative_path) || stringValue(snapshot.path) || item.source_object_key
  );
}

function searchBasis(item: UnpackExecutionItem): string {
  const identity = asRecord(item.media_identity);
  const external = Array.isArray(identity.external_ids)
    ? identity.external_ids.filter(
        (entry): entry is Record<string, unknown> => typeof entry === 'object' && entry !== null,
      )
    : [];
  const imdb = external.find((entry) => entry.namespace === 'imdb');
  if (imdb) return 'IMDb ' + stringValue(imdb.value);
  const douban = external.find((entry) => entry.namespace === 'douban');
  if (douban) return '豆瓣 ' + stringValue(douban.value);
  return '影片名';
}

function itemCanReview(item: UnpackExecutionItem): boolean {
  return item.review_allowed;
}

function itemCanRetry(item: UnpackExecutionItem): boolean {
  return ['match', 'resume'].includes(unpackItemRetryAvailability(item));
}

function itemRetryLabel(item: UnpackExecutionItem): string {
  if (unpackItemRetryAvailability(item) === 'resume') return '继续执行';
  const state = asRecord(item.auxiliary_state);
  if (
    item.status === 'MATCH_ERROR' &&
    item.has_external_operations &&
    item.last_error_code === 'UNPACK_AUXILIARY_CONFLICT' &&
    state.state === 'DOWNLOADING'
  ) {
    return '核对后续跑';
  }
  return item.status === 'MATCH_ERROR' &&
    [
      'DOWNLOADER_UNAVAILABLE',
      'DOWNLOADER_CONNECTION_FAILED',
      'SITE_UNAVAILABLE',
      'SITE_RATE_LIMITED',
    ].includes(item.last_error_code ?? '') &&
    state.state === 'DOWNLOADING'
    ? '继续补齐'
    : '重新匹配';
}

async function retryItem(item: UnpackExecutionItem): Promise<void> {
  try {
    const result = await retryUnpackItemMatch(item.id, item.version);
    ElMessage.success(
      result.item_status === 'AUXILIARY_FETCHING'
        ? '已恢复原有辅助文件补齐，将校验已下载文件，不会重复添加种子'
        : result.item_status === 'EXECUTING'
          ? '已保留原执行计划和操作记录，等待执行器重新核对后继续'
          : '已重新进入匹配队列',
    );
    await loadExecutionItems(itemCursor.value);
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  }
}

async function openItemDiagnostics(item: UnpackExecutionItem): Promise<void> {
  const ticket = diagnosticsGuard.open(item.id);
  diagnosticsVisible.value = true;
  diagnosticsLoading.value = true;
  diagnosticsItemName.value = mediaTitle(item);
  diagnostics.value = null;
  try {
    const result = await getUnpackItemDiagnostics(item.id);
    if (diagnosticsGuard.isCurrent(ticket)) diagnostics.value = result;
  } catch (caught) {
    if (diagnosticsGuard.isCurrent(ticket)) ElMessage.error(toApiProblem(caught).message);
  } finally {
    if (diagnosticsGuard.isCurrent(ticket)) diagnosticsLoading.value = false;
  }
}

function itemCanDelete(item: UnpackExecutionItem): boolean {
  return !['MATCH_PENDING', 'MATCHING'].includes(item.status);
}

async function deleteExecutionItem(item: UnpackExecutionItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '确认从本次拆包执行记录中移除影片“' +
        mediaTitle(item) +
        '”？删除只影响 PackBreaker 的影片记录，不会删除磁盘文件或下载器种子。已有的外部操作记录会独立保存以便追溯。',
      '删除影片记录',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
    );
  } catch {
    return;
  }
  try {
    await deleteUnpackExecutionItem(item.id, item.version);
    ElMessage.success('影片记录已删除，原始媒体文件未受影响');
    // Deleting a row invalidates previously remembered cursor anchors.
    itemCursorHistory.value = [];
    itemPageIndex.value = 1;
    await loadExecutionItems();
    await refresh();
  } catch (error) {
    ElMessage.error(toApiProblem(error).message);
  }
}

async function openReview(item: UnpackExecutionItem): Promise<void> {
  reviewLoading.value = true;
  reviewItem.value = item;
  reviewVisible.value = true;
  try {
    const result = await listUnpackItemCandidates(item.id);
    reviewCandidates.value = result;
    selectedCandidateId.value = result.default_candidate_id ?? '';
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
    reviewVisible.value = false;
  } finally {
    reviewLoading.value = false;
  }
}

function openManualCandidate(): void {
  if (!reviewItem.value || !reviewCandidates.value) return;
  manualSourceMode.value = 'URL';
  manualSourceUrl.value = '';
  manualTorrentFile.value = null;
  manualCandidatePreview.value = null;
  manualSiteId.value = manualSiteOptions.value[0]?.id ?? '';
  manualCandidateVisible.value = true;
}

function resetManualCandidate(): void {
  manualCandidatePreview.value = null;
  manualSourceUrl.value = '';
  manualTorrentFile.value = null;
  manualCandidatePreviewLoading.value = false;
  manualCandidateSaving.value = false;
}

function onManualTorrentFileChange(event: Event): void {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0] ?? null;
  if (file && !file.name.toLowerCase().endsWith('.torrent')) {
    ElMessage.warning('请选择 .torrent 种子文件');
    input.value = '';
    manualTorrentFile.value = null;
    manualCandidatePreview.value = null;
    return;
  }
  if (file && file.size > 32 * 1024 * 1024) {
    ElMessage.warning('种子文件不能超过 32 MiB');
    input.value = '';
    manualTorrentFile.value = null;
    manualCandidatePreview.value = null;
    return;
  }
  manualTorrentFile.value = file;
  manualCandidatePreview.value = null;
}

function manualCandidateInput() {
  const candidates = reviewCandidates.value;
  if (!candidates) throw new Error('审核候选已过期，请重新打开审核弹窗');
  if (!manualSiteId.value) throw new Error('请选择当前任务扫描范围内的站点');
  if (manualSourceMode.value === 'URL') {
    if (!manualSourceUrl.value.trim()) throw new Error('请输入种子详情页 URL');
    return {
      siteId: manualSiteId.value,
      generation: candidates.generation,
      itemVersion: candidates.item_version,
      sourceUrl: manualSourceUrl.value.trim(),
    };
  }
  if (!manualTorrentFile.value) throw new Error('请选择 .torrent 种子文件');
  return {
    siteId: manualSiteId.value,
    generation: candidates.generation,
    itemVersion: candidates.item_version,
    file: manualTorrentFile.value,
  };
}

async function previewManualCandidate(): Promise<void> {
  const item = reviewItem.value;
  if (!item || manualCandidatePreviewLoading.value || manualCandidateSaving.value) return;
  let payload: ManualCandidateInput;
  try {
    payload = manualCandidateInput();
  } catch (caught) {
    ElMessage.warning(caught instanceof Error ? caught.message : '请完善手动添加信息');
    return;
  }
  manualCandidatePreviewLoading.value = true;
  manualCandidatePreview.value = null;
  try {
    manualCandidatePreview.value = await previewManualUnpackCandidate(item.id, payload);
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    manualCandidatePreviewLoading.value = false;
  }
}

function manualCandidateScoreBreakdown(): string[] {
  const preview = manualCandidatePreview.value;
  if (!preview) return [];
  const evidence = asRecord(preview.evidence);
  const dimensions = evidence.dimensions;
  const labels: Record<string, string> = {
    EXTERNAL_ID: '外部 ID',
    TITLE: '标题',
    YEAR_EPISODE: '年份/季集',
    RELEASE_ATTRIBUTES: '发布属性',
    TOTAL_SIZE: '总大小',
    FILE_LIST: '文件列表',
    RELEASE_GROUP: '制作组',
  };
  const result: string[] = [];
  if (typeof evidence.evidence_coverage_bps === 'number') {
    result.push('证据覆盖 ' + Math.round(evidence.evidence_coverage_bps / 100) + '%');
  }
  if (!Array.isArray(dimensions)) return result;
  return result.concat(
    dimensions.flatMap((value) => {
      const dimension = asRecord(value);
      const key = dimension.dimension;
      const label = typeof key === 'string' ? labels[key] : undefined;
      const earned = dimension.earned;
      const maximum = dimension.maximum;
      if (!label || typeof earned !== 'number' || typeof maximum !== 'number') return [];
      const summary =
        maximum === 0 ? label + ' 未计分' : label + ' ' + earned.toFixed(1) + '/' + maximum;
      const detail = typeof dimension.evidence === 'string' ? dimension.evidence : '';
      return [summary + (detail ? '（' + detail + '）' : '')];
    }),
  );
}

async function confirmManualCandidate(): Promise<void> {
  const item = reviewItem.value;
  const preview = manualCandidatePreview.value;
  if (!item || !preview || manualCandidateSaving.value) return;
  if (preview.hard_conflicts.length) {
    ElMessage.warning('候选存在硬冲突，不能直接批准；请检查年份、季集或外部 ID');
    return;
  }
  let payload: ManualCandidateInput;
  try {
    payload = manualCandidateInput();
  } catch (caught) {
    ElMessage.warning(caught instanceof Error ? caught.message : '请重新填写手动添加信息');
    return;
  }
  manualCandidateSaving.value = true;
  try {
    const added = await addManualUnpackCandidate(
      item.id,
      payload,
      preview.metainfo_digest,
      preview.payload_sha256,
    );
    const refreshed = await listUnpackItemCandidates(item.id);
    reviewCandidates.value = refreshed;
    selectedCandidateId.value = added.candidate_id;
    manualCandidateVisible.value = false;
    await submitReview('APPROVE');
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    manualCandidateSaving.value = false;
  }
}

function candidateBlocked(candidate: UnpackMatchCandidate): boolean {
  const conflicts = asRecord(candidate.evidence).hard_conflicts;
  return Array.isArray(conflicts) && conflicts.length > 0;
}

function hardConflictText(value: string): string {
  const labels: Record<string, string> = {
    EXTERNAL_ID_CONFLICT: '外部 ID 冲突',
    YEAR_CONFLICT: '年份冲突',
    EPISODE_CONFLICT: '季集冲突',
  };
  return labels[value] ?? '关键信息冲突';
}

function candidateEvidenceCoverageBps(candidate: UnpackMatchCandidate): number | null {
  const coverage = asRecord(candidate.evidence).evidence_coverage_bps;
  return typeof coverage === 'number' && Number.isFinite(coverage) ? coverage : null;
}

function candidateNeedsEvidenceReview(candidate: UnpackMatchCandidate): boolean {
  const coverage = candidateEvidenceCoverageBps(candidate);
  return coverage !== null && coverage < 6500;
}

function candidateSourceMappingTitle(candidate: UnpackMatchCandidate): string | null {
  const mapping = asRecord(asRecord(candidate.evidence).source_mapping);
  return mapping.matched === true && typeof mapping.media_title === 'string'
    ? mapping.media_title
    : null;
}

function candidateEvidence(candidate: UnpackMatchCandidate): string[] {
  const evidence = asRecord(candidate.evidence);
  const result: string[] = [];
  const mappingTitle = candidateSourceMappingTitle(candidate);
  if (mappingTitle) result.push(`目录映射：${mappingTitle}`);
  const exact = asRecord(evidence.exact);
  const exactLabels: Record<string, string> = {
    title: '标题一致',
    external_id: '外部 ID 一致',
    year: '年份一致',
    episode: '季集一致',
    resolution: '分辨率一致',
    source_medium: '来源一致',
    codec: '编码一致',
    hdr: 'HDR 一致',
    audio: '音频一致',
    release_group: '制作组一致',
    size: '大小一致',
  };
  const mismatchLabels: Record<string, string> = {
    title: '标题差异',
    external_id: '外部 ID 不一致',
    year: '年份不一致',
    episode: '季集不一致',
    resolution: '分辨率不同',
    source_medium: '来源不同',
    codec: '编码不同',
    hdr: 'HDR 不同',
    audio: '音频不同',
    release_group: '制作组不同',
    size: '大小不一致',
  };
  for (const [key, label] of Object.entries(exactLabels)) {
    if (exact[key] === true) result.push(label);
    else if (exact[key] === false && mismatchLabels[key]) result.push(mismatchLabels[key]);
  }
  const conflicts = evidence.hard_conflicts;
  if (Array.isArray(conflicts)) {
    for (const item of conflicts) {
      if (typeof item === 'string') result.push(hardConflictText(item));
    }
  }
  return [...new Set(result)];
}

function candidateScoreBreakdown(candidate: UnpackMatchCandidate): string[] {
  const labels: Record<string, string> = {
    EXTERNAL_ID: '外部 ID',
    TITLE: '标题',
    YEAR_EPISODE: '年份/季集',
    RELEASE_ATTRIBUTES: '发布属性',
    TOTAL_SIZE: '总大小',
    FILE_LIST: '文件列表',
    RELEASE_GROUP: '制作组',
  };
  const evidence = asRecord(candidate.evidence);
  const dimensions = evidence.dimensions;
  const result: string[] = [];
  const coverage = evidence.evidence_coverage_bps;
  if (typeof coverage === 'number') result.push(`证据覆盖 ${Math.round(coverage / 100)}%`);
  if (!Array.isArray(dimensions)) return result;
  const details = dimensions.flatMap((value) => {
    const dimension = asRecord(value);
    const key = dimension.dimension;
    const label = typeof key === 'string' ? labels[key] : undefined;
    const earned = dimension.earned;
    const maximum = dimension.maximum;
    if (!label || typeof earned !== 'number' || typeof maximum !== 'number') return [];
    const score = maximum === 0 ? `${label} 未计分` : `${label} ${earned.toFixed(1)}/${maximum}`;
    const detail = typeof dimension.evidence === 'string' ? dimension.evidence : '';
    return [`${score}${detail ? `（${detail}）` : ''}`];
  });
  return [...result, ...details];
}

function candidateDetailUrl(candidate: UnpackMatchCandidate): string | null {
  const site = sites.value.find((item) => item.id === candidate.site_id);
  if (!site || !/^[0-9]+$/.test(candidate.candidate_key)) return null;
  try {
    const base = new URL(site.base_url);
    if (base.protocol !== 'https:' && base.protocol !== 'http:') return null;
    const detail = new URL('/details.php', base.origin);
    detail.searchParams.set('id', candidate.candidate_key);
    return detail.toString();
  } catch {
    return null;
  }
}

function openCandidateDetail(candidate: UnpackMatchCandidate): void {
  const url = candidateDetailUrl(candidate);
  if (!url) {
    ElMessage.warning('无法生成该候选的站点详情链接');
    return;
  }
  window.open(url, '_blank', 'noopener,noreferrer');
}

function candidateVerificationText(candidate: UnpackMatchCandidate): string {
  if (candidate.verification_status === 'VERIFIED') return '内容校验通过';
  if (candidate.verification_status === 'VERIFYING') return '内容校验中';
  if (candidate.verification_status === 'MISMATCH') return '内容不一致';
  if (candidate.verification_status === 'UNAVAILABLE') return '当前不可用';
  return '尚未校验';
}

async function submitReview(decision: 'APPROVE' | 'NO_MATCH'): Promise<void> {
  const item = reviewItem.value;
  const candidates = reviewCandidates.value;
  if (!item || !candidates) return;
  if (decision === 'APPROVE' && !selectedCandidateId.value) {
    ElMessage.warning('请选择一个候选');
    return;
  }
  reviewSaving.value = true;
  try {
    await reviewUnpackItem(
      item.id,
      {
        decision,
        candidate_id: decision === 'APPROVE' ? selectedCandidateId.value : null,
        generation: candidates.generation,
      },
      {
        itemVersion: candidates.item_version,
        idempotencyKey: createClientNonce(),
      },
    );
    ElMessage.success(decision === 'APPROVE' ? '审核已确认，影片将继续内容校验' : '已标记为无匹配');
    reviewVisible.value = false;
    await loadExecutionItems(itemCursor.value);
    await refresh();
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    reviewSaving.value = false;
  }
}

function reviewSourceMeta(): string[] {
  const item = reviewItem.value;
  if (!item) return [];
  const identity = asRecord(item.media_identity);
  const values: string[] = [];
  const external = Array.isArray(identity.external_ids) ? identity.external_ids : [];
  for (const raw of external) {
    const entry = asRecord(raw);
    const namespace = stringValue(entry.namespace);
    const value = stringValue(entry.value);
    if (namespace && value)
      values.push(
        (namespace === 'imdb' ? 'IMDb' : namespace === 'douban' ? '豆瓣' : namespace) +
          ': ' +
          value,
      );
  }
  for (const key of ['resolution', 'release_source', 'codec', 'release_group']) {
    const value = stringValue(identity[key]);
    if (value) values.push(value);
  }
  const totalSize = identity.total_size;
  if (typeof totalSize === 'number') values.push(formatBytes(totalSize));
  return values;
}

onMounted(() => {
  void refresh();
  const container = unpackTableContainer.value;
  if (!container) return;
  const updateWidth = () => {
    unpackTableContainerWidth.value = container.clientWidth;
  };
  updateWidth();
  if (typeof ResizeObserver !== 'undefined') {
    unpackTableResizeObserver = new ResizeObserver(updateWidth);
    unpackTableResizeObserver.observe(container);
  }
});
</script>

<template>
  <section class="v2-task-center">
    <div class="toolbar">
      <div>
        <h2>数据拆包</h2>
      </div>
    </div>

    <div class="unpack-filter-toolbar">
      <div class="filters">
        <el-select
          v-model="typeFilter"
          clearable
          placeholder="全部类型"
          style="width: 130px"
          @change="searchFilters"
        >
          <el-option label="手动拆包" value="MANUAL" />
          <el-option label="监控拆包" value="MONITOR" />
          <el-option label="模板拆包" value="TEMPLATE" />
        </el-select>
        <el-select
          v-model="statusFilter"
          clearable
          placeholder="全部状态"
          style="width: 150px"
          @change="searchFilters"
        >
          <el-option label="待执行" value="PENDING_EXECUTION" />
          <el-option label="匹配中" value="MATCHING" />
          <el-option label="待人工审核" value="REVIEW_REQUIRED" />
          <el-option label="内容校验中" value="CONTENT_VERIFYING" />
          <el-option label="执行中" value="EXECUTING" />
          <el-option label="下载器校验中" value="CLIENT_VERIFYING" />
          <el-option label="已完成" value="COMPLETED" />
          <el-option label="完成但有异常" value="COMPLETED_WITH_ERRORS" />
          <el-option label="失败" value="FAILED" />
        </el-select>
        <el-input
          v-model="keyword"
          clearable
          placeholder="搜索任务名称 / 来源"
          style="width: 230px"
          @keydown.enter.prevent="searchFilters"
          @clear="searchFilters"
        >
          <template #prefix><Search :size="14" /></template>
        </el-input>
      </div>
      <div class="toolbar-actions">
        <el-button :loading="loading" @click="refresh"><RefreshCw :size="15" />刷新</el-button>
        <el-button type="primary" @click="openCreate()"><Plus :size="15" />新增任务</el-button>
      </div>
    </div>
    <div
      ref="unpackTableContainer"
      class="table-card"
      v-loading="loading"
      :data-actions-mode="showInlineDefinitionActions ? 'inline' : 'overflow'"
    >
      <el-empty v-if="!loading && !visibleRows.length" description="暂无数据拆包任务" />
      <el-table v-else :data="visibleRows" class="unpack-table">
        <el-table-column label="任务名称" min-width="190" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="primary-cell">
              <b>{{ row.definition.name }}</b
              ><small>{{ formatTime(row.definition.updated_at) }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="105">
          <template #default="{ row }">{{
            row.definition.task_kind === 'TEMPLATE'
              ? '模板拆包'
              : unpackTriggerLabel(row.definition.trigger_kind)
          }}</template>
        </el-table-column>
        <el-table-column label="来源" min-width="190" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="primary-cell">
              <b>{{ sourceText(row.definition) }}</b
              ><small>{{ unpackSourceKindLabel(row.definition.source_kind) }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="扫描站点" min-width="155" show-overflow-tooltip>
          <template #default="{ row }">{{ siteText(row.definition) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="135">
          <template #default="{ row }"
            ><el-tag :type="statusTag(row)" effect="light">{{
              taskStatusText(row)
            }}</el-tag></template
          >
        </el-table-column>
        <el-table-column label="匹配进度" min-width="130">
          <template #default="{ row }">
            <div class="progress-cell">
              <span>{{ progressText(row.execution) }}</span>
              <el-progress
                v-if="row.execution?.total_count"
                :percentage="progressPercent(row.execution)"
                :stroke-width="6"
                :show-text="false"
              />
            </div>
          </template>
        </el-table-column>
        <el-table-column label="结果" min-width="210" show-overflow-tooltip>
          <template #default="{ row }">{{ resultText(row) }}</template>
        </el-table-column>
        <el-table-column
          label="操作"
          :width="definitionActionColumnWidth"
          fixed="right"
          class-name="unpack-actions-column"
        >
          <template #default="{ row }">
            <div class="task-row-actions">
              <el-button
                v-if="showExecuteAction(row)"
                link
                type="success"
                @click="runDefinition(row)"
              >
                <Play :size="14" />执行
              </el-button>
              <el-button v-if="showViewAction(row)" link type="primary" @click="openExecution(row)">
                <Eye :size="14" />查看
              </el-button>
              <el-button
                v-if="showRetryAction(row)"
                link
                type="warning"
                :loading="retryingDefinitionId === row.definition.id"
                :disabled="retryingDefinitionId !== null"
                @click="openRetryDialog(row)"
                ><RotateCcw :size="14" />重试</el-button
              >
              <template v-if="showInlineDefinitionActions">
                <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
                <el-button
                  link
                  type="danger"
                  :disabled="!definitionCanDelete(row)"
                  :title="definitionDeleteDisabledReason(row)"
                  @click="deleteDefinition(row)"
                  >删除</el-button
                >
              </template>
              <el-dropdown
                v-else
                class="task-more-dropdown"
                trigger="click"
                placement="bottom-end"
                @command="(command: string) => handleDefinitionMenu(command, row)"
              >
                <el-button link type="primary" class="task-more-trigger" aria-label="更多操作">
                  <Ellipsis :size="18" />
                </el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="edit">编辑</el-dropdown-item>
                    <el-dropdown-item
                      command="delete"
                      divided
                      :disabled="!definitionCanDelete(row)"
                      :title="definitionDeleteDisabledReason(row)"
                      >删除</el-dropdown-item
                    >
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog
      v-model="retryDialogVisible"
      title="重试拆包影片"
      width="min(520px, 94vw)"
      class="unpack-retry-dialog"
      destroy-on-close
      :close-on-click-modal="!retryingDefinitionId"
      :show-close="!retryingDefinitionId"
      @closed="retryTargetRow = null"
    >
      <el-form label-position="top">
        <el-form-item label="拆包状态" required>
          <el-select
            v-model="selectedRetryStatuses"
            multiple
            collapse-tags
            collapse-tags-tooltip
            clearable
            filterable
            style="width: 100%"
            placeholder="选择要重新匹配的影片状态（可多选）"
            :disabled="retryingDefinitionId !== null"
            aria-label="拆包状态"
          >
            <el-option
              v-for="statusOption in retryStatusOptions"
              :key="statusOption.value"
              :label="statusOption.label"
              :value="statusOption.value"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button
          type="primary"
          :disabled="selectedRetryStatuses.length === 0"
          :loading="retryingDefinitionId !== null"
          @click="retryDefinition"
          >重试</el-button
        >
      </template>
    </el-dialog>

    <el-dialog
      v-model="createVisible"
      :title="editingDefinition ? '编辑数据拆包任务' : '新增数据拆包任务'"
      width="min(1040px, 96vw)"
      destroy-on-close
    >
      <div class="unpack-wizard" data-testid="unpack-create-wizard">
        <nav class="wizard-progress" aria-label="任务配置步骤">
          <button
            v-for="wizardStep in createWizardSteps"
            :key="wizardStep.number"
            type="button"
            class="wizard-step"
            :class="{
              'is-active': createStep === wizardStep.number,
              'is-complete': createStep > wizardStep.number,
            }"
            :disabled="wizardStep.number >= createStep"
            :aria-current="createStep === wizardStep.number ? 'step' : undefined"
            @click="goToWizardStep(wizardStep.number)"
          >
            <span class="wizard-step-number">{{
              createStep > wizardStep.number ? '✓' : wizardStep.number
            }}</span>
            <span class="wizard-step-label">{{ wizardStep.label }}</span>
          </button>
        </nav>

        <header class="wizard-step-heading">
          <span class="wizard-step-badge">{{ createStep }}</span>
          <div>
            <h3>{{ currentWizardStep.title }}</h3>
            <p>{{ currentWizardStep.description }}</p>
          </div>
        </header>

        <section v-if="createStep === 1" class="form-section wizard-content wizard-basics">
          <el-form-item label="任务名称" required>
            <el-input
              v-model="draft.name"
              maxlength="100"
              show-word-limit
              placeholder="请输入任务名称，例如：电影库手动拆包"
            />
            <small>用于区分不同的拆包任务，方便识别和管理。</small>
          </el-form-item>

          <div class="wizard-field-label">任务类型 <span class="required-mark">*</span></div>
          <div class="trigger-card-grid" role="group" aria-label="任务类型">
            <button
              type="button"
              class="trigger-card"
              :class="{ 'is-selected': draft.taskKind === 'MANUAL' }"
              :aria-pressed="draft.taskKind === 'MANUAL'"
              @click="chooseTaskKind('MANUAL')"
            >
              <span class="trigger-card-icon"><Play :size="20" /></span>
              <span class="trigger-card-copy">
                <b>手动拆包</b>
                <small>一次性扫描并执行任务，适合临时整理媒体。</small>
              </span>
              <span class="trigger-card-radio" aria-hidden="true"></span>
            </button>
            <button
              type="button"
              class="trigger-card"
              :class="{ 'is-selected': draft.taskKind === 'MONITOR' }"
              :aria-pressed="draft.taskKind === 'MONITOR'"
              @click="chooseTaskKind('MONITOR')"
            >
              <span class="trigger-card-icon"><Clock3 :size="20" /></span>
              <span class="trigger-card-copy">
                <b>监控拆包</b>
                <small>按设定的时间或规则定期扫描并执行。</small>
              </span>
              <span class="trigger-card-radio" aria-hidden="true"></span>
            </button>
            <button
              type="button"
              class="trigger-card"
              :class="{ 'is-selected': draft.taskKind === 'TEMPLATE' }"
              :aria-pressed="draft.taskKind === 'TEMPLATE'"
              @click="chooseTaskKind('TEMPLATE')"
            >
              <span class="trigger-card-icon"><CheckCircle2 :size="20" /></span>
              <span class="trigger-card-copy">
                <b>模板拆包</b>
                <small>复用已入库的种子元数据，匹配阶段优先查询本地模板库。</small>
              </span>
              <span class="trigger-card-radio" aria-hidden="true"></span>
            </button>
          </div>

          <el-form-item
            v-if="draft.taskKind !== 'TEMPLATE'"
            class="select-trigger-field"
            label="扫描站点"
            required
          >
            <el-select
              :automatic-dropdown="false"
              @click.stop
              v-model="draft.siteIds"
              multiple
              collapse-tags
              collapse-tags-tooltip
              clearable
              placeholder="请选择需要扫描的站点"
              style="width: 100%"
            >
              <el-option
                v-for="site in enabledSites"
                :key="site.id"
                :label="site.name"
                :value="site.id"
              />
            </el-select>
            <small>从已启用的站点中选择，可同时选择多个站点。</small>
          </el-form-item>

          <el-form-item v-if="draft.taskKind === 'TEMPLATE'" label="拆包模板" required>
            <el-select
              v-model="draft.templateId"
              filterable
              clearable
              placeholder="请选择已完成入库的模板"
              style="width: 100%"
              @change="onTemplateChange"
            >
              <el-option
                v-for="template in readyTemplates"
                :key="template.id"
                :label="templateOptionLabel(template)"
                :value="template.id"
              />
            </el-select>
            <small
              >官方模板绑定唯一站点；模板匹配时优先使用已记录的影片目录—种子关系，但仍执行候选评分与内容验证。</small
            >
            <div v-if="selectedTemplate" class="template-site-summary">
              <span>模板绑定站点：</span>
              <el-tag v-if="selectedTemplateBoundSiteLabel" size="small">{{
                selectedTemplateBoundSiteLabel
              }}</el-tag>
              <span v-else>当前实例没有对应的已启用站点配置</span>
              <span>已关联 {{ selectedTemplate.mapped_media_count }} 个影片目录</span>
              <span v-if="templateMatchingStatusLabel(selectedTemplate)">
                匹配状态：{{ templateMatchingStatusLabel(selectedTemplate) }}
              </span>
            </div>
            <small v-if="!readyTemplates.length"
              >暂无可用模板。请先通过受控模板导入接口完成种子元数据入库。</small
            >
          </el-form-item>

          <el-form-item v-if="selectedTemplate" label="使用的站点配置" required>
            <el-select
              :model-value="draft.siteIds[0] ?? ''"
              filterable
              clearable
              placeholder="请选择模板绑定的站点配置"
              style="width: 100%"
              @change="onTemplateSiteChange"
            >
              <el-option
                v-for="site in selectedTemplateSites"
                :key="site.id"
                :label="site.name"
                :value="site.id"
              />
            </el-select>
            <small>只能选择模板绑定站点类型的已启用配置，不能跨站执行。</small>
          </el-form-item>

          <section v-if="draft.triggerKind === 'MONITOR'" class="cron-panel">
            <div class="cron-panel-heading">
              <b><Clock3 :size="16" /> 执行时间</b>
              <span>仅监控拆包需要设置</span>
            </div>
            <el-form-item label="Cron 表达式" required>
              <el-input v-model="draft.cronExpression" placeholder="例如：0 3 * * *" />
              <small>使用 5 段 Cron 表达式定义扫描频率。</small>
            </el-form-item>
            <div class="preset-row cron-preset-row" aria-label="Cron 快捷设置">
              <el-button @click="applyCronPreset('*/5 * * * *')">每 5 分钟</el-button>
              <el-button @click="applyCronPreset('*/10 * * * *')">每 10 分钟</el-button>
              <el-button @click="applyCronPreset('0 * * * *')">每小时</el-button>
              <el-button @click="applyCronPreset('0 3 * * *')">每天 03:00</el-button>
              <el-button @click="applyCronPreset('0 3 * * 1')">每周一 03:00</el-button>
            </div>
          </section>
        </section>

        <div v-else-if="createStep === 2" class="wizard-content wizard-source-target">
          <section class="form-section">
            <div class="wizard-section-title">
              <div>
                <h4>数据来源</h4>
                <p>选择要扫描的目录；模板拆包也可以指定一个已完成的下载器种子。</p>
              </div>
            </div>
            <div class="choice-row source-choice">
              <el-button
                :type="draft.sourceKind === 'DIRECTORY' ? 'primary' : 'default'"
                @click="chooseSource('DIRECTORY')"
                ><FolderOpen :size="15" />目录</el-button
              >
              <el-button
                v-if="draft.triggerKind === 'MONITOR' || draft.taskKind === 'TEMPLATE'"
                :type="draft.sourceKind === 'DOWNLOADER' ? 'primary' : 'default'"
                @click="chooseSource('DOWNLOADER')"
                ><Play :size="15" />下载器</el-button
              >
            </div>
            <div class="form-grid">
              <template v-if="draft.sourceKind === 'DIRECTORY'">
                <el-form-item label="来源目录" required>
                  <div class="inline-field">
                    <el-input
                      v-model="draft.sourceDirectory"
                      readonly
                      placeholder="请选择来源目录"
                    />
                    <el-button @click="openDirectoryPicker('SOURCE')"
                      ><FolderOpen :size="14" />选择目录</el-button
                    >
                  </div>
                </el-form-item>
              </template>
              <template v-else>
                <el-form-item class="select-trigger-field" label="来源下载器" required>
                  <el-select
                    :automatic-dropdown="false"
                    v-model="draft.sourceDownloaderId"
                    placeholder="请选择下载器"
                    style="width: 100%"
                    @click.stop
                    @change="onSourceDownloaderChange"
                  >
                    <el-option
                      v-for="item in enabledDownloaders"
                      :key="item.id"
                      :label="item.name"
                      :value="item.id"
                    />
                  </el-select>
                </el-form-item>
                <el-form-item
                  v-if="draft.taskKind === 'TEMPLATE'"
                  class="select-trigger-field source-seed-field"
                  required
                >
                  <template #label>
                    <span class="source-seed-label-row">
                      <span class="extension-label-title">来源种子</span>
                      <span class="source-seed-hint"
                        ><Info
                          :size="12"
                          aria-hidden="true"
                        />仅读取已完成种子；创建任务不会修改下载器任务。</span
                      >
                    </span>
                  </template>
                  <el-select
                    v-model="draft.sourceTorrentHash"
                    filterable
                    :loading="sourceTorrentsLoading"
                    :disabled="!draft.sourceDownloaderId"
                    placeholder="请选择一个已完成的种子"
                    style="width: 100%"
                    @visible-change="
                      (visible: boolean) => visible && loadSourceTorrents(draft.sourceDownloaderId)
                    "
                    @change="onSourceTorrentChange"
                  >
                    <el-option
                      v-for="torrent in sourceTorrents"
                      :key="torrent.torrent_hash"
                      :label="`${torrent.name} · ${formatBytes(torrent.size_bytes)}`"
                      :value="torrent.torrent_hash"
                    />
                  </el-select>
                </el-form-item>
                <template v-else>
                  <el-form-item label="任务名称包含">
                    <el-input
                      v-model="draft.downloaderNameContains"
                      clearable
                      placeholder="例如：Movie / 电影"
                    />
                  </el-form-item>
                  <el-form-item label="分类">
                    <el-input
                      v-model="draft.downloaderCategories"
                      clearable
                      placeholder="多个分类使用逗号分隔"
                    />
                  </el-form-item>
                  <el-form-item label="标签">
                    <el-input
                      v-model="draft.downloaderTags"
                      clearable
                      placeholder="多个标签使用逗号分隔"
                    />
                  </el-form-item>
                </template>
              </template>
            </div>
          </section>

          <section class="form-section">
            <div class="wizard-section-title">
              <div>
                <h4>输出目标</h4>
                <p>设置文件整理后的目标位置和最终辅种下载器。</p>
              </div>
            </div>
            <div class="form-grid">
              <el-form-item label="输出目录" required>
                <div class="inline-field">
                  <el-input v-model="draft.outputDirectory" readonly placeholder="请选择输出目录" />
                  <el-button @click="openDirectoryPicker('OUTPUT')"
                    ><FolderOpen :size="14" />选择目录</el-button
                  >
                </div>
              </el-form-item>
              <el-form-item class="select-trigger-field" label="目标下载器" required>
                <el-select
                  :automatic-dropdown="false"
                  v-model="draft.targetDownloaderId"
                  placeholder="请选择最终辅种下载器"
                  @click.stop
                  @change="onTargetDownloaderChange"
                >
                  <el-option
                    v-for="item in enabledDownloaders"
                    :key="item.id"
                    :label="item.name"
                    :value="item.id"
                  />
                </el-select>
              </el-form-item>
              <el-form-item class="select-trigger-field" label="目标下载器分类">
                <el-select
                  :automatic-dropdown="false"
                  @click.stop
                  v-model="draft.targetCategory"
                  filterable
                  allow-create
                  clearable
                  default-first-option
                  :loading="targetDownloaderOptionsLoading"
                  placeholder="选择或输入分类"
                  @visible-change="
                    (visible: boolean) =>
                      visible && loadTargetDownloaderOptions(draft.targetDownloaderId)
                  "
                >
                  <el-option
                    v-for="value in targetCategoryOptions"
                    :key="value"
                    :label="value"
                    :value="value"
                  />
                </el-select>
              </el-form-item>
              <el-form-item class="select-trigger-field" label="目标下载器标签">
                <el-select
                  :automatic-dropdown="false"
                  @click.stop
                  v-model="draft.targetTags"
                  multiple
                  filterable
                  allow-create
                  clearable
                  default-first-option
                  :loading="targetDownloaderOptionsLoading"
                  placeholder="选择或输入标签"
                  @visible-change="
                    (visible: boolean) =>
                      visible && loadTargetDownloaderOptions(draft.targetDownloaderId)
                  "
                >
                  <el-option
                    v-for="value in targetTagOptions"
                    :key="value"
                    :label="value"
                    :value="value"
                  />
                </el-select>
              </el-form-item>
            </div>
            <small v-if="selectedTargetDownloader?.type === 'TRANSMISSION'" class="hint">
              Transmission 不区分分类和标签，分类与标签会作为标签一并写入。
            </small>
          </section>
        </div>

        <section
          v-else-if="createStep === 3 && draft.taskKind !== 'TEMPLATE'"
          class="form-section wizard-content wizard-file-filters"
        >
          <div class="wizard-section-title">
            <div>
              <h4>基础过滤条件</h4>
              <p>只处理符合以下文件类型、大小和名称规则的媒体。</p>
            </div>
          </div>
          <div class="form-grid">
            <el-form-item class="extension-filter-item" required>
              <template #label>
                <span class="extension-label-row">
                  <span class="extension-label-title">后缀名白名单</span>
                  <span class="extension-label-hint"
                    ><Info :size="12" aria-hidden="true" />多个后缀使用逗号分隔。</span
                  >
                </span>
              </template>
              <el-input v-model="draft.extensions" placeholder=".mkv, .mp4, .ts" />
            </el-form-item>
            <el-form-item label="最小大小（MB）">
              <el-input-number
                v-model="draft.minSizeMb"
                :min="0"
                controls-position="right"
                style="width: 100%"
              />
            </el-form-item>
            <el-form-item label="最大大小（MB）">
              <el-input-number
                v-model="draft.maxSizeMb"
                :min="0"
                controls-position="right"
                placeholder="不限"
                style="width: 100%"
              />
            </el-form-item>
            <el-form-item label="包含名称">
              <el-input v-model="draft.includeName" clearable placeholder="留空表示不限" />
            </el-form-item>
            <el-form-item label="排除名称">
              <el-input
                v-model="draft.excludeNames"
                clearable
                placeholder="逗号分隔，例如 sample, trailer, SP., featurette, bonus feature"
              />
            </el-form-item>
          </div>
          <el-checkbox v-model="draft.includeSubdirectories">扫描时包含子目录</el-checkbox>

          <div class="advanced-rule-block">
            <button
              type="button"
              class="advanced-toggle"
              :class="{ 'is-expanded': advancedRulesExpanded }"
              :aria-expanded="advancedRulesExpanded"
              aria-controls="unpack-advanced-rule-content"
              @click="toggleAdvancedRules"
            >
              <span class="advanced-toggle-icon">⚙</span>
              <span class="advanced-toggle-copy">
                <b>高级执行规则（默认折叠）</b>
                <small>{{ advancedRulesSummary }}</small>
              </span>
              <span class="advanced-toggle-action"
                >{{ advancedRulesExpanded ? '收起' : '展开' }}
                <span aria-hidden="true">{{ advancedRulesExpanded ? '⌃' : '⌄' }}</span></span
              >
            </button>
            <div
              v-if="advancedRulesExpanded"
              id="unpack-advanced-rule-content"
              class="advanced-rule-content"
            >
              <div class="form-grid three">
                <el-form-item label="自动匹配">
                  <el-switch
                    v-model="draft.autoMatchEnabled"
                    active-text="开启"
                    inactive-text="关闭"
                  />
                </el-form-item>
                <el-form-item label="自动重试">
                  <el-switch v-model="draft.retryEnabled" active-text="开启" inactive-text="关闭" />
                </el-form-item>
                <el-form-item label="存放方式">
                  <el-select v-model="draft.storageMode">
                    <el-option label="硬链接" value="HARDLINK" />
                    <el-option label="软链接" value="SYMLINK" />
                    <el-option label="复制" value="COPY" />
                  </el-select>
                </el-form-item>
                <el-form-item v-if="draft.autoMatchEnabled" label="自动匹配阈值（%）">
                  <el-input-number
                    v-model="draft.autoMatchPercent"
                    :min="0"
                    :max="100"
                    :step="0.1"
                    :precision="1"
                  />
                </el-form-item>
                <el-form-item label="重试次数">
                  <el-input-number v-model="draft.maxRetries" :min="0" :max="10" />
                </el-form-item>
              </div>
              <small class="advanced-rule-note"
                >这些选项会影响匹配与文件物化行为；请按实际存储和下载器环境选择。</small
              >
            </div>
          </div>
        </section>

        <div v-else class="wizard-content wizard-review-grid">
          <section class="review-card">
            <header>
              <b>基本信息</b
              ><el-button link type="primary" @click="goToWizardStep(1)">编辑</el-button>
            </header>
            <dl class="review-list">
              <div>
                <dt>任务名称</dt>
                <dd>{{ draft.name || '—' }}</dd>
              </div>
              <div>
                <dt>任务类型</dt>
                <dd>
                  {{
                    draft.taskKind === 'TEMPLATE'
                      ? '模板拆包'
                      : draft.triggerKind === 'MONITOR'
                        ? '监控拆包'
                        : '手动拆包'
                  }}
                </dd>
              </div>
              <div>
                <dt>扫描站点</dt>
                <dd>{{ selectedSiteNames.join('、') || '—' }}</dd>
              </div>
              <div v-if="draft.triggerKind === 'MONITOR'">
                <dt>执行时间</dt>
                <dd>
                  <code>{{ draft.cronExpression || '—' }}</code>
                </dd>
              </div>
            </dl>
          </section>

          <section v-if="draft.taskKind === 'TEMPLATE'" class="review-card">
            <header>
              <b>模板与扫描统计</b>
              <el-button link type="primary" @click="goToWizardStep(1)">编辑模板</el-button>
            </header>
            <dl class="review-list">
              <div>
                <dt>模板</dt>
                <dd>{{ selectedTemplate?.name || '—' }}</dd>
              </div>
              <div>
                <dt>模板版本</dt>
                <dd>{{ draft.templateVersion ? `v${draft.templateVersion}` : '—' }}</dd>
              </div>
              <div>
                <dt>模板种子数</dt>
                <dd>{{ selectedTemplate?.seed_count ?? 0 }}</dd>
              </div>
              <div>
                <dt>来源扫描结果</dt>
                <dd>
                  {{
                    templatePreview?.sourceKind === 'DIRECTORY'
                      ? `${templatePreview.discoveredCount ?? 0} 个媒体文件`
                      : templatePreview?.sourceKind === 'DOWNLOADER'
                        ? '1 个已完成种子；媒体文件将在执行时扫描'
                        : '未扫描'
                  }}
                </dd>
              </div>
              <div
                v-if="
                  templatePreview?.sourceSizeBytes !== null &&
                  templatePreview?.sourceSizeBytes !== undefined
                "
              >
                <dt>来源种子大小</dt>
                <dd>{{ formatBytes(templatePreview.sourceSizeBytes) }}</dd>
              </div>
              <div>
                <dt>匹配方式</dt>
                <dd>优先读取本地模板库；本步骤不查询站点、不执行下载器操作</dd>
              </div>
            </dl>
          </section>

          <section class="review-card">
            <header>
              <b>来源与目标</b
              ><el-button link type="primary" @click="goToWizardStep(2)">编辑</el-button>
            </header>
            <dl class="review-list">
              <div>
                <dt>来源类型</dt>
                <dd>{{ draft.sourceKind === 'DIRECTORY' ? '目录' : '下载器' }}</dd>
              </div>
              <div v-if="draft.sourceKind === 'DIRECTORY'">
                <dt>来源目录</dt>
                <dd>{{ draft.sourceDirectory || '—' }}</dd>
              </div>
              <div v-else>
                <dt>来源下载器</dt>
                <dd>{{ selectedSourceDownloader?.name || '—' }}</dd>
              </div>
              <div v-if="draft.sourceKind === 'DOWNLOADER' && draft.taskKind === 'TEMPLATE'">
                <dt>来源种子</dt>
                <dd>{{ selectedSourceTorrent?.name || draft.downloaderNameContains || '—' }}</dd>
              </div>
              <div v-if="draft.sourceKind === 'DOWNLOADER' && draft.taskKind !== 'TEMPLATE'">
                <dt>名称 / 分类 / 标签</dt>
                <dd>
                  {{
                    [
                      draft.downloaderNameContains || '不限',
                      draft.downloaderCategories || '不限',
                      draft.downloaderTags || '不限',
                    ].join(' / ')
                  }}
                </dd>
              </div>
              <div>
                <dt>输出目录</dt>
                <dd>{{ draft.outputDirectory || '—' }}</dd>
              </div>
              <div>
                <dt>目标下载器</dt>
                <dd>{{ selectedTargetDownloader?.name || '—' }}</dd>
              </div>
              <div>
                <dt>目标分类</dt>
                <dd>{{ draft.targetCategory || '—' }}</dd>
              </div>
              <div>
                <dt>目标标签</dt>
                <dd>{{ draft.targetTags.join('、') || '—' }}</dd>
              </div>
            </dl>
          </section>

          <section v-if="draft.taskKind !== 'TEMPLATE'" class="review-card">
            <header>
              <b>文件过滤</b>
              <el-button link type="primary" @click="goToWizardStep(3)">编辑</el-button>
            </header>
            <dl class="review-list">
              <div>
                <dt>后缀白名单</dt>
                <dd>{{ draft.extensions || '—' }}</dd>
              </div>
              <div>
                <dt>最小 / 最大大小</dt>
                <dd>
                  {{ draft.minSizeMb === null ? '不限' : draft.minSizeMb + ' MB' }} /
                  {{ draft.maxSizeMb === null ? '不限' : draft.maxSizeMb + ' MB' }}
                </dd>
              </div>
              <div>
                <dt>包含名称</dt>
                <dd>{{ draft.includeName || '不限' }}</dd>
              </div>
              <div>
                <dt>排除名称</dt>
                <dd>{{ draft.excludeNames || '无' }}</dd>
              </div>
              <div>
                <dt>包含子目录</dt>
                <dd>{{ draft.includeSubdirectories ? '是' : '否' }}</dd>
              </div>
            </dl>
          </section>

          <section v-if="draft.taskKind !== 'TEMPLATE'" class="review-card">
            <header>
              <b>高级执行规则</b>
              <el-button
                link
                type="primary"
                @click="
                  goToWizardStep(3);
                  advancedRulesExpanded = true;
                "
                >编辑</el-button
              >
            </header>
            <dl class="review-list">
              <div>
                <dt>自动匹配</dt>
                <dd>{{ draft.autoMatchEnabled ? '开启' : '关闭' }}</dd>
              </div>
              <div>
                <dt>自动重试</dt>
                <dd>{{ draft.retryEnabled ? '开启' : '关闭' }}</dd>
              </div>
              <div>
                <dt>重试次数</dt>
                <dd>{{ draft.maxRetries }} 次</dd>
              </div>
              <div v-if="draft.autoMatchEnabled">
                <dt>自动匹配阈值</dt>
                <dd>{{ draft.autoMatchPercent }}%</dd>
              </div>
              <div>
                <dt>存放方式</dt>
                <dd>{{ storageModeLabel }}</dd>
              </div>
            </dl>
          </section>
          <div class="wizard-confirm-note">
            <CheckCircle2 :size="16" /><span v-if="draft.taskKind === 'TEMPLATE'"
              >确认后只保存任务配置；执行时优先使用本地模板匹配，本步骤不会触发站点查询或下载器写操作。</span
            ><span v-else>确认后将保存任务配置。手动目录任务仍会进入原有的拆包范围确认流程。</span>
          </div>
        </div>
      </div>
      <template #footer>
        <div class="wizard-footer">
          <span class="wizard-footer-note"
            >第 {{ createStep }} / {{ createWizardSteps.length }} 步<span
              v-if="createStep === createWizardSteps.length"
            >
              · 创建后可在任务列表中查看执行状态</span
            ></span
          >
          <div class="wizard-footer-actions">
            <el-button v-if="createStep === 1" @click="createVisible = false">取消</el-button>
            <el-button v-else @click="previousWizardStep">上一步</el-button>
            <el-button
              v-if="createStep < createWizardSteps.length"
              type="primary"
              :loading="templatePreviewLoading"
              @click="nextWizardStep"
              >下一步 <span aria-hidden="true">→</span></el-button
            >
            <el-button v-else type="primary" :loading="saving" @click="saveDraft">{{
              editingDefinition ? '保存' : '创建任务'
            }}</el-button>
          </div>
        </div>
      </template>
    </el-dialog>
    <el-dialog v-model="scopeConfirmVisible" title="确认拆包范围" width="560px">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="是否将当前目录下符合过滤条件的所有影视文件加入本任务？"
      />
      <p class="dialog-help">
        选择“是”保存为全部影视文件；选择“否”先扫描目录，再由你勾选需要处理的影片。
      </p>
      <template #footer>
        <el-button :loading="scanLoading" @click="chooseMediaFiles">否，选择影片</el-button>
        <el-button type="primary" :loading="saving" @click="useAllMedia">是，全部处理</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="mediaSelectVisible"
      title="选择需要拆包的影视文件"
      width="min(1120px, 97vw)"
      destroy-on-close
    >
      <div class="scan-toolbar">
        <el-input
          v-model="scanKeyword"
          clearable
          placeholder="搜索影片名称 / 路径"
          @keyup.enter="loadScanItems()"
        />
        <el-select v-model="scanExtension" clearable placeholder="全部后缀"
          ><el-option
            v-for="value in splitCsv(draft.extensions)"
            :key="value"
            :label="value.toUpperCase()"
            :value="value"
        /></el-select>
        <el-select v-model="scanResolution" clearable placeholder="全部分辨率"
          ><el-option label="2160p" value="2160p" /><el-option
            label="1080p"
            value="1080p" /><el-option label="720p" value="720p"
        /></el-select>
        <el-select v-model="scanSelectedFilter" clearable placeholder="全部选择状态"
          ><el-option label="已勾选" value="selected" /><el-option
            label="未勾选"
            value="unselected"
        /></el-select>
        <el-button
          :loading="scanLoading"
          @click="
            scanCursorHistory = [];
            loadScanItems();
          "
          ><Search :size="14" />筛选</el-button
        >
      </div>
      <div class="selection-summary">
        <span>已发现 {{ scanDiscoveredCount }} 个影视文件 · 已选择 {{ scanSelectedCount }} 个</span>
        <div>
          <el-button @click="setVisibleSelection(true)">勾选当前结果</el-button
          ><el-button @click="setVisibleSelection(false)">取消当前结果</el-button>
        </div>
      </div>
      <el-table :data="scanItems" v-loading="scanLoading" max-height="470">
        <el-table-column label="选择" width="70"
          ><template #default="{ row }"
            ><el-checkbox
              :model-value="row.selected"
              @change="toggleScanItem(row, $event)" /></template
        ></el-table-column>
        <el-table-column label="影片" min-width="360" show-overflow-tooltip
          ><template #default="{ row }"
            ><div class="primary-cell">
              <b>{{ row.filename }}</b
              ><small>{{ row.relative_path }}</small>
            </div></template
          ></el-table-column
        >
        <el-table-column prop="resolution" label="分辨率" width="100"
          ><template #default="{ row }">{{ row.resolution || '—' }}</template></el-table-column
        >
        <el-table-column prop="extension" label="后缀" width="90" />
        <el-table-column label="大小" width="120"
          ><template #default="{ row }">{{
            formatBytes(row.size_bytes)
          }}</template></el-table-column
        >
      </el-table>
      <div class="pager">
        <el-button :disabled="!scanCursorHistory.length" @click="previousScanPage">上一页</el-button
        ><el-button :disabled="!scanNextCursor" @click="nextScanPage">下一页</el-button>
      </div>
      <template #footer>
        <span class="footer-count">已选择 {{ scanSelectedCount }} / {{ scanDiscoveredCount }}</span>
        <el-button
          @click="
            mediaSelectVisible = false;
            createVisible = true;
          "
          >取消</el-button
        >
        <el-button
          type="primary"
          :disabled="!canSaveSelectedMedia"
          :loading="saving"
          @click="saveSelectedMedia"
          >保存任务</el-button
        >
      </template>
    </el-dialog>

    <el-dialog
      v-model="diagnosticsVisible"
      width="min(1080px, 96vw)"
      class="execution-detail-dialog"
    >
      <template #header>
        <div class="execution-detail-header">
          <strong>执行明细</strong>
          <span :title="diagnosticsItemName">{{ diagnosticsItemName }}</span>
        </div>
      </template>
      <div v-loading="diagnosticsLoading" class="execution-detail-body">
        <template v-if="diagnostics && detailSummary">
          <div class="execution-detail-overview" aria-label="执行结果概览">
            <div
              class="detail-summary-card"
              :class="{
                result: [
                  'EXECUTION_ERROR',
                  'MATCH_ERROR',
                  'MATCH_TIMEOUT',
                  'CONTENT_MISMATCH',
                ].includes(diagnostics.status),
              }"
              data-detail-card="result"
            >
              <div class="detail-card-icon"><Info :size="20" /></div>
              <div class="detail-card-copy">
                <small>当前结果</small>
                <b>{{ unpackItemStatusLabel(diagnostics.status) }}</b>
                <span>来自本次任务记录</span>
              </div>
            </div>
            <div class="detail-summary-card" data-detail-card="stage">
              <div class="detail-card-icon"><FolderOpen :size="20" /></div>
              <div class="detail-card-copy">
                <small>停止阶段</small>
                <b>{{ unpackExecutionStageLabel(diagnostics.execution_stage) }}</b>
                <span>历史记录的最后阶段</span>
              </div>
            </div>
            <div
              class="detail-summary-card"
              :class="{ caution: diagnostics.requires_manual_reconciliation }"
              data-detail-card="risk"
            >
              <div class="detail-card-icon"><Info :size="20" /></div>
              <div class="detail-card-copy">
                <small>风险状态</small>
                <b>{{
                  diagnostics.requires_manual_reconciliation ? '需要人工核对' : '未标记强制核对'
                }}</b>
                <span>不代表实时核验已通过</span>
              </div>
            </div>
            <div class="detail-summary-card" data-detail-card="advice">
              <div class="detail-card-icon"><Info :size="20" /></div>
              <div class="detail-card-copy">
                <small>可执行建议</small>
                <b>{{ detailSummary.recommendationLabel }}</b>
                <span>不会自动操作下载器</span>
              </div>
            </div>
          </div>

          <section
            class="execution-detail-explanation"
            :class="{
              warning: diagnostics.requires_manual_reconciliation,
              'is-success':
                !diagnostics.last_error_code && !diagnostics.requires_manual_reconciliation,
            }"
            aria-label="执行结果说明"
          >
            <div class="detail-explanation-heading">
              <Info :size="20" />
              <h3>{{ detailSummary.title }}</h3>
            </div>
            <p>{{ detailSummary.description }}</p>
            <p class="detail-recommendation"><b>建议：</b>{{ detailSummary.recommendation }}</p>
            <p v-if="diagnostics.acceptance_read_only" class="detail-security-warning">
              这是只读验收任务：不可创建文件，也不可向下载器添加、校验或启动种子。
            </p>
            <p
              v-if="diagnostics.plan_status === 'BLOCKED' || diagnostics.plan_status === 'INVALID'"
              class="detail-security-warning"
            >
              上次执行计划存在问题：{{
                diagnostics.plan_blocked_reasons.map(unpackPlanBlockReasonLabel).join('、') ||
                unpackPlanStatusLabel(diagnostics.plan_status)
              }}。不能凭历史记录跳过安全检查。
            </p>
            <p v-if="diagnostics.requires_manual_reconciliation" class="detail-security-warning">
              部分文件或下载器操作的结果仍不明确，必须先核对，不允许直接重复执行这些操作。
            </p>
          </section>

          <section class="execution-detail-stages" aria-label="执行阶段">
            <div class="detail-section-title">
              <h3>执行阶段</h3>
            </div>
            <ol v-if="detailTimeline.length" class="detail-timeline">
              <li
                v-for="stage in detailTimeline"
                :key="stage.key"
                :class="['detail-step', stage.result]"
              >
                <span class="detail-step-indicator">
                  <CheckCircle2 v-if="stage.result === 'recorded'" :size="18" />
                  <Info v-else :size="18" />
                </span>
                <div class="detail-step-copy">
                  <b>{{ stage.label }}</b>
                  <small>{{ stage.note }}</small>
                </div>
                <time v-if="stage.at">{{ formatTime(stage.at) }}</time>
                <span v-else class="detail-step-no-time">无单独时间记录</span>
              </li>
            </ol>
            <div v-else class="detail-no-history">还没有保存的外部操作记录。</div>
            <details
              v-if="diagnostics.requires_manual_reconciliation || diagnostics.operations_truncated"
              class="detail-extra-guidance"
            >
              <summary>查看更多核对说明</summary>
              <ul>
                <li
                  v-for="(step, index) in unpackReconciliationGuidance(diagnostics, null)"
                  :key="index"
                >
                  {{ step }}
                </li>
              </ul>
            </details>
          </section>

          <section class="execution-detail-records" aria-label="操作记录">
            <div class="detail-section-title">
              <h3>操作记录</h3>
              <small
                >共 {{ diagnostics.operations.length }} 条{{
                  diagnostics.operations_truncated ? '（仅显示前 200 条）' : ''
                }}
                · 均为历史记录</small
              >
            </div>
            <el-table
              v-if="diagnostics.operations.length"
              :data="diagnostics.operations"
              class="detail-history-table"
              size="small"
              max-height="255"
              border
            >
              <el-table-column label="操作" min-width="220">
                <template #default="{ row }">
                  <div class="detail-operation-label" :title="row.operation_type">
                    <b>{{ unpackOperationTypeLabel(row.operation_type) }}</b>
                    <small>{{ formatTime(row.updated_at) }}</small>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="结果" width="133">
                <template #default="{ row }">
                  <el-tag
                    size="small"
                    :type="
                      row.status === 'APPLIED'
                        ? 'success'
                        : row.status === 'FAILED'
                          ? 'danger'
                          : 'warning'
                    "
                    >{{ unpackOperationStatusLabel(row.status) }}</el-tag
                  >
                </template>
              </el-table-column>
              <el-table-column label="位置或下载器" min-width="260">
                <template #default="{ row }">
                  <el-tooltip
                    :content="row.target_path || row.downloader_id || '未记录位置'"
                    placement="top"
                    :show-after="250"
                    popper-class="execution-detail-target-tooltip"
                  >
                    <div class="detail-target">
                      {{ row.target_path || row.downloader_id || '未记录位置' }}
                    </div>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="备注" min-width="215">
                <template #default="{ row }">
                  <span class="detail-record-note" :title="row.last_error_code || ''">
                    {{ unpackExecutionDetailRecordNote(row) }}
                    <span v-if="row.recorded_size !== null">
                      · {{ formatBytes(row.recorded_size) }}</span
                    >
                  </span>
                </template>
              </el-table-column>
            </el-table>
            <div v-else class="detail-no-history">暂无文件或下载器操作记录。</div>
            <small v-if="diagnostics.operations_truncated" class="detail-history-disclaimer">
              记录已截断，不能据此认定全部操作都已核对。
            </small>
          </section>
        </template>
      </div>
      <template #footer>
        <div class="detail-footer">
          <el-button @click="diagnosticsVisible = false">关闭</el-button>
        </div>
      </template>
    </el-dialog>

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
        <el-tree
          lazy
          :load="loadTreeNode"
          node-key="token"
          :props="{ label: 'name', isLeaf: () => false }"
          highlight-current
          @node-click="onTreeNodeClick"
        />
        <aside>
          <b>当前选择</b>
          <p>{{ selectedTreeNode?.path || '请选择目录节点' }}</p>
          <small>目录选择使用后端授权根与 opaque token；保存时仍会重新校验路径权限。</small>
        </aside>
      </div>
    </el-dialog>

    <el-dialog
      v-model="executionVisible"
      :title="'执行详情 · ' + (activeDefinition?.name ?? '')"
      width="min(1180px, 97vw)"
    >
      <div v-if="activeExecution" class="execution-detail">
        <div class="pipeline">
          <div
            v-for="(step, index) in pipelineSteps"
            :key="step"
            :class="[
              'pipeline-step',
              { done: index < activePipelineIndex, active: index === activePipelineIndex },
            ]"
          >
            <b>{{ index + 1 }}. {{ step }}</b>
          </div>
        </div>
        <div class="metric-grid">
          <div>
            <b>{{ activeExecution.total_count }}</b
            ><small>影片总数</small>
          </div>
          <div>
            <b>{{ activeExecution.matched_auto_count }}</b
            ><small>自动匹配</small>
          </div>
          <div>
            <b>{{ activeExecution.no_match_count }}</b
            ><small>无匹配</small>
          </div>
          <div>
            <b>{{ activeExecution.review_count }}</b
            ><small>待人工审核</small>
          </div>
          <div>
            <b>{{ activeExecution.content_verified_count }}</b
            ><small>内容校验通过</small>
          </div>
          <div>
            <b>{{ activeExecution.timeout_count }}</b
            ><small>匹配超时</small>
          </div>
          <div>
            <b>{{ activeExecution.error_count + activeExecution.content_mismatch_count }}</b
            ><small>异常 / 不一致</small>
          </div>
        </div>
        <div class="execution-state">
          <el-tag
            :type="
              activeExecution.status === 'COMPLETED'
                ? 'success'
                : activeExecution.status === 'FAILED'
                  ? 'danger'
                  : activeExecution.status === 'COMPLETED_WITH_ERRORS'
                    ? 'danger'
                    : activeExecution.status === 'REVIEW_REQUIRED'
                      ? 'warning'
                      : 'primary'
            "
            >{{ unpackExecutionStatusLabel(activeExecution.status) }}</el-tag
          ><span
            >完成 {{ activeExecution.completed_count }} / {{ activeExecution.total_count }}</span
          >
        </div>
        <div class="execution-item-filters">
          <el-select
            v-model="executionItemStatusFilter"
            clearable
            placeholder="全部影片状态"
            style="width: 170px"
            @change="applyExecutionItemFilters"
          >
            <el-option label="无匹配" value="NO_MATCH" />
            <el-option label="匹配中" value="MATCHING" />
            <el-option label="匹配错误" value="MATCH_ERROR" />
            <el-option label="匹配超时" value="MATCH_TIMEOUT" />
            <el-option label="人工审核" value="REVIEW_REQUIRED" />
            <el-option label="自动匹配" value="MATCHED_AUTO" />
            <el-option label="已完成" value="COMPLETED" />
            <el-option label="执行异常" value="EXECUTION_ERROR" />
          </el-select>
          <el-input
            v-model="executionItemKeyword"
            clearable
            placeholder="搜索影片名或来源路径"
            style="width: 265px"
            @keyup.enter="applyExecutionItemFilters"
            @clear="applyExecutionItemFilters"
          >
            <template #prefix><Search :size="14" /></template>
          </el-input>
          <el-button @click="applyExecutionItemFilters">筛选</el-button>
          <el-button :loading="executionLoading" @click="refreshExecutionItems">
            <RefreshCw :size="14" />刷新
          </el-button>
        </div>
        <el-table :data="executionItems" v-loading="executionLoading" max-height="500">
          <el-table-column label="影片" min-width="270" show-overflow-tooltip
            ><template #default="{ row }"
              ><div class="primary-cell">
                <b>{{ mediaTitle(row) }}</b
                ><small>{{
                  stringValue(asRecord(row.source_snapshot).relative_path) || row.source_object_key
                }}</small>
              </div></template
            ></el-table-column
          >
          <el-table-column label="搜索依据" min-width="130"
            ><template #default="{ row }">{{ searchBasis(row) }}</template></el-table-column
          >
          <el-table-column label="内容校验" min-width="145"
            ><template #default="{ row }">{{
              unpackVerificationLevelLabel(row.content_verification_level)
            }}</template></el-table-column
          >
          <el-table-column label="状态" width="145"
            ><template #default="{ row }"
              ><el-tag :type="itemStatusTag(row.status)">{{
                unpackItemStatusSummary(row)
              }}</el-tag></template
            ></el-table-column
          >
          <el-table-column label="错误说明" min-width="380">
            <template #default="{ row }">
              <el-tooltip
                v-if="unpackItemExternalRiskLabel(row)"
                :content="unpackItemExternalRiskLabel(row) || ''"
                placement="top"
              >
                <el-tag type="danger" size="small">需人工对账 · 禁止重试</el-tag>
              </el-tooltip>
              <el-tooltip
                :content="unpackErrorLabel(row.last_error_code, row.last_error_message)"
                placement="top"
                :show-after="250"
              >
                <span class="item-error-explanation">{{
                  row.last_error_code
                    ? unpackErrorLabel(row.last_error_code, row.last_error_message)
                    : '—'
                }}</span>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="400" fixed="right">
            <template #default="{ row }">
              <div class="task-row-actions">
                <el-button
                  v-if="row.has_external_operations || unpackItemExternalRiskLabel(row)"
                  link
                  type="primary"
                  @click="openItemDiagnostics(row)"
                  >执行明细</el-button
                >
                <div v-if="itemCanReview(row)" class="review-action-group">
                  <span
                    v-if="row.highest_candidate_score_bps !== null"
                    class="best-candidate-score"
                    title="本轮候选队列中的最高匹配度，仅供审核参考，不代表已完成内容校验"
                    >最高 {{ (row.highest_candidate_score_bps / 100).toFixed(1) }}%</span
                  >
                  <el-button link type="primary" @click="openReview(row)">审核</el-button>
                </div>
                <el-button v-if="itemCanRetry(row)" link type="warning" @click="retryItem(row)"
                  ><RotateCcw :size="14" />{{ itemRetryLabel(row) }}</el-button
                >
                <el-tooltip
                  v-else-if="unpackItemRetryAvailability(row) === 'reconcile'"
                  content="这部影片已经进行过文件或下载器操作，现有记录不足以证明可以安全继续，请先查看执行明细。"
                  placement="top"
                >
                  <span class="blocked-retry"><el-button link disabled>暂不可重试</el-button></span>
                </el-tooltip>
                <el-button
                  link
                  type="danger"
                  :disabled="!itemCanDelete(row)"
                  :title="
                    itemCanDelete(row)
                      ? '删除影片记录，不删除媒体或种子；外部操作历史会留存'
                      : '影片待匹配或匹配中，不能删除'
                  "
                  @click="deleteExecutionItem(row)"
                  >删除</el-button
                >
              </div>
            </template>
          </el-table-column>
        </el-table>
        <div class="pager">
          <span>第 {{ itemPageIndex }} 页 · 每页</span>
          <el-select :model-value="itemPageSize" style="width: 95px" @change="changeItemPageSize">
            <el-option v-for="size in [20, 50, 100]" :key="size" :label="size" :value="size" />
          </el-select>
          <el-button :disabled="!itemCursorHistory.length" @click="previousItemPage"
            >上一页</el-button
          ><el-button :disabled="!itemNextCursor" @click="nextItemPage">下一页</el-button>
        </div>
      </div>
      <template #footer><el-button @click="executionVisible = false">关闭</el-button></template>
    </el-dialog>

    <el-dialog v-model="reviewVisible" width="min(1040px, 96vw)">
      <template #header>
        <div class="review-dialog-header">
          <strong class="review-dialog-title"
            >人工审核候选 · {{ reviewItem ? mediaTitle(reviewItem) : '' }}</strong
          >
        </div>
      </template>
      <div v-loading="reviewLoading">
        <div v-if="reviewItem" class="source-box">
          <strong>{{ mediaTitle(reviewItem) }}</strong>
          <div class="source-meta">
            <span v-for="item in reviewSourceMeta()" :key="item">{{ item }}</span>
          </div>
        </div>
        <div class="review-heading">
          <div class="review-heading-description">
            <b>候选列表</b><small>默认选中匹配度最高的一条，但必须点击确认后才会继续。</small>
          </div>
          <div class="review-heading-actions">
            <el-tag type="warning">人工审核</el-tag>
            <el-button
              type="primary"
              plain
              size="small"
              class="manual-add-candidate-trigger"
              :disabled="reviewLoading || reviewSaving || !reviewItem || !reviewCandidates"
              @click="openManualCandidate"
              ><Plus :size="14" />手动添加</el-button
            >
          </div>
        </div>
        <div class="candidate-list">
          <label
            v-for="candidate in reviewCandidates?.candidates ?? []"
            :key="candidate.id"
            :class="[
              'candidate-card',
              {
                selected: selectedCandidateId === candidate.id,
                blocked: candidateBlocked(candidate),
              },
            ]"
          >
            <el-radio
              v-model="selectedCandidateId"
              :value="candidate.id"
              :disabled="candidateBlocked(candidate)"
            />
            <div class="candidate-main">
              <div class="candidate-title-row">
                <b>{{ candidate.title }}</b>
                <el-button
                  link
                  size="small"
                  class="candidate-detail-button"
                  :disabled="!candidateDetailUrl(candidate)"
                  @click.stop.prevent="openCandidateDetail(candidate)"
                  ><ExternalLink :size="12" />详情</el-button
                >
              </div>
              <div class="candidate-meta">
                <span>{{
                  sites.find((site) => site.id === candidate.site_id)?.name ?? '站点已删除'
                }}</span>
                <span v-if="candidate.imdb_id">IMDb {{ candidate.imdb_id }}</span>
                <span v-if="candidate.douban_id">豆瓣 {{ candidate.douban_id }}</span>
                <span>{{ formatBytes(candidate.size_bytes) }}</span>
                <span v-if="candidate.seeders !== null">做种 {{ candidate.seeders }}</span>
              </div>
              <div class="evidence-row">
                <span v-for="value in candidateEvidence(candidate)" :key="value">{{ value }}</span>
              </div>
              <div class="score-breakdown" aria-label="匹配度构成">
                <span v-for="value in candidateScoreBreakdown(candidate)" :key="value">{{
                  value
                }}</span>
              </div>
              <small v-if="candidateNeedsEvidenceReview(candidate)" class="score-evidence-warning">
                证据覆盖不足；匹配度只反映已有字段一致性，因此需要人工审核。
              </small>
              <small>{{ candidateVerificationText(candidate) }}</small>
            </div>
            <div class="score">
              <b>{{ (candidate.score_bps / 100).toFixed(1) }}%</b
              ><small>{{
                candidateSourceMappingTitle(candidate)
                  ? '按映射影片评分'
                  : candidate.is_exact_match
                    ? '精确证据成立'
                    : candidateBlocked(candidate)
                      ? '存在硬冲突'
                      : '候选'
              }}</small>
            </div>
          </label>
        </div>
        <el-empty
          v-if="!reviewLoading && !reviewCandidates?.candidates.length"
          description="当前没有候选"
        />
      </div>
      <template #footer>
        <el-button :loading="reviewSaving" @click="submitReview('NO_MATCH')">标记无匹配</el-button>
        <el-button
          type="primary"
          :loading="reviewSaving"
          :disabled="!selectedCandidateId"
          @click="submitReview('APPROVE')"
          ><CheckCircle2 :size="14" />确认所选候选并继续</el-button
        >
      </template>
    </el-dialog>
    <el-dialog
      v-model="manualCandidateVisible"
      title="手动添加种子候选"
      width="min(820px, 95vw)"
      append-to-body
      :close-on-click-modal="false"
      @closed="resetManualCandidate"
    >
      <div class="manual-candidate-form" v-loading="manualCandidatePreviewLoading">
        <div class="manual-candidate-fields">
          <label class="manual-field-label">种子来源</label>
          <el-radio-group
            v-model="manualSourceMode"
            :disabled="manualCandidateSaving || manualCandidatePreviewLoading"
            @change="manualCandidatePreview = null"
          >
            <el-radio-button value="URL">种子详情页 URL</el-radio-button>
            <el-radio-button value="FILE">种子文件</el-radio-button>
          </el-radio-group>
          <label class="manual-field-label">站点</label>
          <el-select
            v-model="manualSiteId"
            placeholder="选择当前拆包任务扫描范围内的站点"
            :disabled="manualCandidateSaving || manualCandidatePreviewLoading"
            @change="manualCandidatePreview = null"
          >
            <el-option
              v-for="site in manualSiteOptions"
              :key="site.id"
              :label="site.name"
              :value="site.id"
            />
          </el-select>
          <template v-if="manualSourceMode === 'URL'">
            <label class="manual-field-label">种子详情页 URL</label>
            <el-input
              v-model="manualSourceUrl"
              clearable
              placeholder="https://站点/details.php?id=12345"
              :disabled="manualCandidateSaving || manualCandidatePreviewLoading"
              @input="manualCandidatePreview = null"
            />
            <small
              >仅接受所选站点自身的 details.php?id=数字
              链接；系统通过已配置的站点适配器读取，不会直接请求任意外部 URL。</small
            >
          </template>
          <template v-else>
            <label class="manual-field-label">种子文件（最大 32 MiB）</label>
            <input
              type="file"
              accept=".torrent,application/x-bittorrent"
              :disabled="manualCandidateSaving || manualCandidatePreviewLoading"
              @change="onManualTorrentFileChange"
            />
            <small v-if="manualTorrentFile"
              >已选择：{{ manualTorrentFile.name }}（{{
                formatBytes(manualTorrentFile.size)
              }}）</small
            >
            <small v-else
              >上传的种子仅在用户确认后保存到私有存储；不会在预览阶段添加下载器任务或写入媒体文件。</small
            >
          </template>
          <div class="manual-candidate-preview-actions">
            <el-button
              type="primary"
              :loading="manualCandidatePreviewLoading"
              :disabled="manualCandidateSaving || !manualSiteId"
              @click="previewManualCandidate"
              >预览并对比</el-button
            >
          </div>
        </div>
        <template v-if="manualCandidatePreview">
          <el-divider content-position="left">种子信息对比</el-divider>
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="扫描站点">{{
              manualCandidatePreview.site_name
            }}</el-descriptions-item>
            <el-descriptions-item label="来源方式">{{
              manualCandidatePreview.source_kind === 'URL' ? '站点 URL' : '上传文件'
            }}</el-descriptions-item>
            <el-descriptions-item label="来源影片">{{
              manualCandidatePreview.source_title
            }}</el-descriptions-item>
            <el-descriptions-item label="种子标题">{{
              manualCandidatePreview.candidate_title
            }}</el-descriptions-item>
            <el-descriptions-item label="种子内部名称">{{
              manualCandidatePreview.torrent_name
            }}</el-descriptions-item>
            <el-descriptions-item label="匹配度"
              >{{ (manualCandidatePreview.score_bps / 100).toFixed(1) }}%</el-descriptions-item
            >
            <el-descriptions-item label="来源大小">{{
              formatBytes(manualCandidatePreview.source_size_bytes)
            }}</el-descriptions-item>
            <el-descriptions-item label="种子总大小">{{
              formatBytes(manualCandidatePreview.total_size_bytes)
            }}</el-descriptions-item>
            <el-descriptions-item label="文件数量">{{
              manualCandidatePreview.file_count
            }}</el-descriptions-item>
            <el-descriptions-item label="种子类型">{{
              manualCandidatePreview.torrent_kind
            }}</el-descriptions-item>
            <el-descriptions-item label="V1 Info Hash">{{
              manualCandidatePreview.v1_info_hash ?? '无'
            }}</el-descriptions-item>
            <el-descriptions-item label="V2 Info Hash">{{
              manualCandidatePreview.v2_info_hash ?? '无'
            }}</el-descriptions-item>
          </el-descriptions>
          <el-alert
            v-if="manualCandidatePreview.hard_conflicts.length"
            type="error"
            :closable="false"
            show-icon
            :title="'检测到硬冲突：' + manualCandidatePreview.hard_conflicts.join('、')"
            description="现有审核安全规则不允许直接批准存在硬冲突的候选，请核对影片年份、季集或外部 ID。"
          />
          <el-alert
            v-else
            type="success"
            :closable="false"
            show-icon
            title="元信息对比完成"
            description="请检查标题、大小、文件清单和分项证据。确认后候选会进入现有内容校验流程；不会跳过内容校验。"
          />
          <div class="manual-score-breakdown">
            <b>匹配度构成</b>
            <span v-for="value in manualCandidateScoreBreakdown()" :key="value">{{ value }}</span>
          </div>
          <div class="manual-file-list">
            <b>种子文件清单（最多预览前 20 项）</b>
            <el-table
              :data="manualCandidatePreview.files.slice(0, 20)"
              size="small"
              max-height="220"
            >
              <el-table-column prop="path" label="相对路径" min-width="260" show-overflow-tooltip />
              <el-table-column label="大小" width="130" align="right">
                <template #default="scope">{{ formatBytes(scope.row.size_bytes) }}</template>
              </el-table-column>
            </el-table>
          </div>
        </template>
      </div>
      <template #footer>
        <el-button @click="manualCandidateVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="manualCandidateSaving"
          :disabled="
            !manualCandidatePreview ||
            manualCandidatePreview.hard_conflicts.length > 0 ||
            manualCandidatePreviewLoading
          "
          @click="confirmManualCandidate"
          ><CheckCircle2 :size="14" />确认并进入内容校验</el-button
        >
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.v2-task-center {
  display: grid;
  gap: 18px;
}
/* Execution details · Scheme A: compact result, plain-language explanation and recorded timeline. */
.execution-detail-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  min-width: 0;
  padding-right: 25px;
}
.execution-detail-header strong {
  flex-shrink: 0;
  font-size: 18px;
  color: var(--el-text-color-primary);
}
.execution-detail-header span {
  color: var(--muted);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}
.execution-detail-body {
  display: grid;
  gap: 19px;
  min-height: 110px;
  max-height: calc(85vh - 155px);
  overflow-y: auto;
  padding-right: 4px;
}
.execution-detail-overview {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}
.detail-summary-card {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
  padding: 14px 12px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface);
}
.detail-card-icon {
  flex: 0 0 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 34px;
  border-radius: 50%;
  background: var(--blue-soft);
  color: var(--blue);
}
.detail-summary-card.result .detail-card-icon {
  background: color-mix(in srgb, #ef4444 10%, var(--surface));
  color: #dc2626;
}
.detail-summary-card.caution .detail-card-icon {
  background: color-mix(in srgb, #f59e0b 13%, var(--surface));
  color: #c06b08;
}
.detail-card-copy {
  display: grid;
  gap: 5px;
  min-width: 0;
}
.detail-card-copy small {
  color: var(--muted);
  font-size: 12px;
}
.detail-card-copy b {
  font-size: 14px;
  line-height: 1.4;
  overflow-wrap: anywhere;
  color: var(--el-text-color-primary);
}
.detail-summary-card.result .detail-card-copy b {
  color: #dc2626;
}
.detail-card-copy span {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.35;
}
.execution-detail-explanation {
  border: 1px solid color-mix(in srgb, #ef4444 24%, var(--line));
  background: color-mix(in srgb, #ef4444 5%, var(--surface));
  border-radius: 13px;
  padding: 15px 18px;
  color: var(--el-text-color-primary);
}
.execution-detail-explanation.warning {
  border-color: color-mix(in srgb, #ea580c 35%, var(--line));
}
.execution-detail-explanation.is-success {
  background: var(--blue-soft);
  border-color: var(--line);
}
.execution-detail-explanation.is-success .detail-explanation-heading,
.execution-detail-explanation.is-success .detail-recommendation b {
  color: var(--blue);
}
.detail-explanation-heading {
  display: flex;
  gap: 10px;
  align-items: center;
  color: #dc2626;
}
.detail-explanation-heading h3,
.detail-section-title h3 {
  margin: 0;
  font-size: 15px;
}
.execution-detail-explanation p {
  margin: 8px 0 0 30px;
  font-size: 13px;
  line-height: 1.65;
}
.execution-detail-explanation .detail-recommendation {
  margin-top: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  background: color-mix(in srgb, #ef4444 7%, var(--surface));
}
.detail-recommendation b {
  color: #dc2626;
}
.execution-detail-explanation .detail-security-warning {
  color: #b45309;
  font-weight: 600;
}
.execution-detail-explanation .detail-live-observation {
  margin: 12px 0 0 30px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface);
  font-size: 12px;
  line-height: 1.6;
}
.detail-section-title {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}
.detail-section-title small,
.detail-history-disclaimer {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.5;
}
.detail-timeline {
  list-style: none;
  margin: 0;
  padding: 4px 0 0;
}
.detail-step {
  display: flex;
  align-items: flex-start;
  gap: 13px;
  position: relative;
  min-height: 57px;
  padding: 0 10px 12px 0;
  color: var(--el-text-color-primary);
}
.detail-step:not(:last-child)::before {
  content: '';
  position: absolute;
  top: 25px;
  bottom: -1px;
  left: 10px;
  width: 2px;
  background: color-mix(in srgb, #16a34a 35%, var(--line));
}
.detail-step.failed {
  margin: 0 0 8px;
  padding: 9px 12px 10px 0;
  border-radius: 9px;
  background: color-mix(in srgb, #ef4444 7%, var(--surface));
}
.detail-step.failed::before,
.detail-step.attention::before {
  background: color-mix(in srgb, #ef4444 35%, var(--line));
}
.detail-step-indicator {
  flex: 0 0 22px;
  z-index: 1;
  color: #16a34a;
  background: var(--surface);
  display: flex;
  align-items: center;
  justify-content: center;
}
.detail-step.failed .detail-step-indicator {
  color: #dc2626;
}
.detail-step.attention .detail-step-indicator {
  color: #d97706;
}
.detail-step-copy {
  display: grid;
  flex: 1;
  gap: 4px;
  min-width: 0;
}
.detail-step-copy b {
  font-size: 13px;
}
.detail-step-copy small {
  color: var(--muted);
  line-height: 1.5;
  font-size: 12px;
}
.detail-step.failed .detail-step-copy b {
  color: #dc2626;
}
.detail-step time,
.detail-step-no-time {
  flex: 0 0 auto;
  font-size: 11px;
  color: var(--muted);
}
.detail-extra-guidance {
  margin: 0 0 9px 35px;
  padding: 9px 12px;
  font-size: 12px;
  color: var(--muted);
  background: var(--blue-soft);
  border-radius: 8px;
}
.detail-extra-guidance summary {
  cursor: pointer;
}
.detail-extra-guidance ul {
  padding-left: 20px;
  line-height: 1.65;
}
.detail-no-history {
  padding: 20px 12px;
  text-align: center;
  color: var(--muted);
  border: 1px dashed var(--line);
  border-radius: 10px;
}
.execution-detail-records {
  min-width: 0;
}
.detail-history-table {
  width: 100%;
  border-radius: 10px;
  overflow: hidden;
}
.detail-operation-label {
  display: grid;
  gap: 4px;
  min-width: 0;
}
.detail-operation-label b {
  font-size: 12px;
  font-weight: 600;
}
.detail-operation-label small {
  font-size: 11px;
  color: var(--muted);
}
.detail-target {
  display: block;
  width: 100%;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  font-size: 12px;
}
.detail-record-note {
  font-size: 12px;
  color: var(--muted);
  line-height: 1.5;
}
.detail-history-disclaimer {
  display: block;
  margin-top: 9px;
}
.detail-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
}
@media (max-width: 900px) {
  .execution-detail-overview {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 540px) {
  .execution-detail-header {
    flex-direction: column;
    gap: 4px;
  }
  .execution-detail-overview {
    grid-template-columns: 1fr;
  }
  .execution-detail-explanation p,
  .execution-detail-explanation .detail-live-observation {
    margin-left: 0;
  }
  .detail-step time {
    font-size: 10px;
  }
  .detail-footer {
    justify-content: flex-end;
  }
}
.review-dialog-header {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 14px;
  padding-right: 36px;
}
.review-dialog-title {
  font-size: 16px;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.review-action-group {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 8px;
  height: 32px;
  line-height: 1;
}
.review-action-group :deep(.el-button) {
  display: inline-flex;
  align-items: center;
  height: 32px;
  line-height: 1;
}
.best-candidate-score {
  display: inline-flex;
  align-items: center;
  height: 32px;
  line-height: 1;
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
}
.blocked-retry {
  display: inline-flex;
  align-items: center;
}
.diagnostics-history-title {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 12px;
  margin: 16px 0 10px;
}
.diagnostics-history-title small {
  color: var(--muted);
}
.diagnostics-target {
  display: block;
  overflow-wrap: anywhere;
  max-height: 3.5em;
  overflow: hidden;
}
.item-error-explanation {
  display: block;
  white-space: normal;
  overflow-wrap: anywhere;
  line-height: 1.5;
  max-height: 4.5em;
  overflow: hidden;
}
.review-heading-actions :deep(.manual-add-candidate-trigger.el-button) {
  color: var(--blue) !important;
  background: var(--surface) !important;
  border-color: color-mix(in srgb, var(--blue) 48%, var(--line)) !important;
}
.review-heading-actions :deep(.manual-add-candidate-trigger.el-button span) {
  color: inherit !important;
}
.review-heading-actions :deep(.manual-add-candidate-trigger.el-button:hover:not(:disabled)) {
  color: #fff !important;
  background: var(--blue) !important;
  border-color: var(--blue) !important;
}

.manual-candidate-form {
  display: grid;
  gap: 14px;
}
.manual-candidate-fields {
  display: grid;
  gap: 8px;
}
.manual-field-label {
  color: var(--el-text-color-regular);
  font-size: 13px;
  font-weight: 600;
}
.manual-candidate-fields small {
  color: var(--el-text-color-secondary);
  line-height: 1.5;
}
.manual-candidate-preview-actions {
  display: flex;
  justify-content: flex-end;
}
.manual-score-breakdown,
.manual-file-list {
  display: grid;
  gap: 8px;
}
.manual-score-breakdown span {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.toolbar,
.table-head,
.selection-summary,
.review-heading,
.execution-state {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
}
.toolbar h2 {
  margin: 0 0 4px;
  font-size: 19px;
}
.toolbar p,
.review-heading small,
.cron-helper small,
.form-section > small {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
}
.toolbar-actions,
.filters,
.choice-row,
.preset-row,
.pager,
.source-meta,
.candidate-meta,
.evidence-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.unpack-filter-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
  padding: 0;
  border: 0;
  background: transparent;
  box-shadow: none;
}
.unpack-filter-toolbar .filters {
  flex: 1 1 600px;
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  box-shadow: none;
}
.unpack-filter-toolbar .toolbar-actions {
  flex: 0 0 auto;
}
.table-card {
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface);
}
.table-head {
  padding: 14px 16px;
  border-bottom: 1px solid var(--line);
  flex-wrap: wrap;
}
.table-head span {
  color: var(--muted);
  font-size: 12px;
}
.unpack-table :deep(.cell) {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.task-row-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  width: 100%;
  min-width: 0;
  white-space: nowrap;
}
.task-row-actions :deep(.el-button) {
  flex-shrink: 0;
  margin: 0;
}
.task-more-trigger {
  width: 32px;
  height: 30px;
  padding: 0;
  border-radius: 6px;
}
.primary-cell {
  display: block;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.primary-cell small {
  margin-left: 7px;
  color: var(--muted);
  font-size: 10px;
}
.progress-cell {
  display: grid;
  gap: 5px;
  min-width: 110px;
}
.table-note {
  padding: 11px 16px;
  border-top: 1px solid var(--line);
  background: #fffbeb;
  color: #92400e;
  font-size: 12px;
}
.form-sections {
  display: grid;
  gap: 14px;
}
.form-section {
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 12px;
}
.form-section.advanced {
  background: color-mix(in srgb, var(--surface) 92%, var(--canvas));
}
.form-section h3 {
  margin: 0 0 14px;
  font-size: 14px;
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 18px;
}
.form-grid.three {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.form-section :deep(.el-form-item) {
  display: grid;
  margin-bottom: 14px;
}
.form-section :deep(.el-form-item__label) {
  justify-content: flex-start;
  margin-bottom: 5px;
}
.source-choice {
  margin-bottom: 14px;
}
.inline-field {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 8px;
  width: 100%;
}
.cron-helper {
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--canvas);
}
.cron-helper > b {
  display: block;
  margin-bottom: 9px;
}
.cron-parts {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
  margin-top: 10px;
}
.cron-parts :deep(.el-form-item) {
  margin: 0;
}
.dialog-help {
  color: var(--muted);
  line-height: 1.7;
}
.scan-toolbar {
  display: grid;
  grid-template-columns: minmax(220px, 1.5fr) repeat(3, minmax(120px, 0.7fr)) auto;
  gap: 10px;
  margin-bottom: 12px;
}
.selection-summary {
  padding: 10px 12px;
  margin-bottom: 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--canvas);
}
.pager {
  justify-content: flex-end;
  padding-top: 12px;
}
.footer-count {
  margin-right: auto;
  color: var(--muted);
}
.directory-picker-header {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 14px;
}
.directory-picker-header > span {
  flex: 0 0 auto;
  font-weight: 600;
}
.directory-picker-header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.tree-picker {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 280px;
  gap: 16px;
  min-height: 420px;
}
.tree-picker :deep(.el-tree) {
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  overflow: auto;
}
.tree-picker aside {
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--canvas);
}
.tree-picker aside p {
  overflow-wrap: anywhere;
}
.tree-picker aside small {
  color: var(--muted);
  line-height: 1.6;
}
.execution-detail {
  display: grid;
  gap: 16px;
}
.pipeline {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 8px;
}
.pipeline-step {
  min-height: 62px;
  padding: 10px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--canvas);
  font-size: 12px;
}
.pipeline-step.done {
  border-color: #bbf7d0;
  background: #f0fdf4;
}
.pipeline-step.active {
  border-color: #bfdbfe;
  background: var(--blue-soft);
  box-shadow: inset 3px 0 0 var(--blue);
}
.metric-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px;
}
.metric-grid > div {
  padding: 11px;
  border: 1px solid var(--line);
  border-radius: 10px;
}
.metric-grid b {
  display: block;
  font-size: 20px;
}
.metric-grid small {
  color: var(--muted);
}
.source-box {
  padding: 14px;
  margin-bottom: 14px;
  border: 1px solid #bfdbfe;
  border-radius: 12px;
  background: var(--blue-soft);
}
.source-box strong {
  display: block;
  margin-bottom: 7px;
}
.source-meta {
  color: var(--muted);
  font-size: 12px;
}
.review-heading {
  margin-bottom: 10px;
}
.review-heading > .review-heading-description {
  display: grid;
  gap: 4px;
}
.review-heading > .review-heading-actions {
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  flex-shrink: 0;
}
.review-heading-actions :deep(.el-button) {
  margin: 0;
}
.candidate-list {
  display: grid;
  gap: 10px;
}
.candidate-card {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr) 110px;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 12px;
  cursor: pointer;
}
.candidate-card.selected {
  border-color: #93c5fd;
  background: var(--blue-soft);
}
.candidate-card.blocked {
  opacity: 0.65;
  cursor: not-allowed;
}
.candidate-main {
  min-width: 0;
}
.candidate-title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.candidate-title-row > b {
  display: block;
  flex: 1;
  min-width: 0;
  margin-bottom: 5px;
  overflow-wrap: anywhere;
}
.candidate-detail-button {
  flex-shrink: 0;
  margin: 0;
}
.candidate-meta {
  color: var(--muted);
  font-size: 12px;
}
.evidence-row {
  margin-top: 8px;
}
.evidence-row span {
  padding: 3px 6px;
  border-radius: 5px;
  background: var(--canvas);
  color: var(--muted);
  font-size: 11px;
}
.score-breakdown {
  display: flex;
  flex-wrap: wrap;
  gap: 5px 8px;
  margin-top: 7px;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.45;
}
.score-breakdown span {
  overflow-wrap: anywhere;
}
.candidate-main > small {
  display: block;
  margin-top: 7px;
  color: var(--muted);
}
.score-evidence-warning {
  color: var(--el-color-warning);
  font-weight: 600;
  line-height: 1.45;
}
.score {
  text-align: right;
}
.score b {
  display: block;
  color: var(--blue);
  font-size: 21px;
}
.score small {
  color: var(--muted);
}
.unpack-wizard {
  display: grid;
  gap: 18px;
  min-width: 0;
}
.wizard-progress {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--line);
}
.wizard-step {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  padding: 5px 3px;
  border: 0;
  background: transparent;
  color: var(--muted);
  text-align: left;
  font: inherit;
}
.wizard-step:not(:disabled) {
  cursor: pointer;
}
.wizard-step:disabled {
  cursor: default;
}
.wizard-step-number {
  display: inline-grid;
  flex: 0 0 28px;
  width: 28px;
  height: 28px;
  place-items: center;
  border: 1px solid var(--line);
  border-radius: 50%;
  background: var(--surface);
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
}
.wizard-step.is-active {
  color: var(--blue);
  font-weight: 700;
}
.wizard-step.is-active .wizard-step-number,
.wizard-step.is-complete .wizard-step-number {
  border-color: var(--blue);
  background: var(--blue);
  color: #fff;
}
.wizard-step.is-complete {
  color: var(--text);
}
.wizard-step-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}
.wizard-step-heading {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: -2px;
}
.wizard-step-badge {
  display: inline-grid;
  flex: 0 0 44px;
  width: 44px;
  height: 44px;
  place-items: center;
  border-radius: 50%;
  background: var(--blue);
  color: #fff;
  font-size: 22px;
  font-weight: 750;
}
.wizard-step-heading h3 {
  margin: 0 0 4px;
  font-size: 18px;
}
.wizard-step-heading p {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.5;
}
.wizard-content {
  min-width: 0;
}
.wizard-basics > .el-form-item {
  margin-bottom: 18px;
}
.wizard-basics :deep(.el-form-item__label) {
  font-weight: 650;
}
.wizard-basics :deep(.el-form-item__content) {
  display: grid;
  gap: 5px;
}
.wizard-basics small,
.advanced-rule-note {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.5;
}
.wizard-field-label {
  margin: 0 0 8px;
  color: var(--text);
  font-size: 13px;
  font-weight: 650;
}
.required-mark {
  color: var(--el-color-danger);
}
.trigger-card-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 18px;
}
.trigger-card {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  min-width: 0;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface);
  color: var(--text);
  text-align: left;
  font: inherit;
  cursor: pointer;
  transition:
    border-color 0.15s ease,
    background 0.15s ease,
    box-shadow 0.15s ease;
}
.trigger-card:hover {
  border-color: var(--blue);
}
.trigger-card.is-selected {
  border-color: var(--blue);
  background: var(--blue-soft);
  box-shadow: inset 0 0 0 1px var(--blue);
}
.trigger-card-icon {
  display: grid;
  flex: 0 0 38px;
  width: 38px;
  height: 38px;
  place-items: center;
  border-radius: 10px;
  background: var(--canvas);
  color: var(--blue);
}
.trigger-card-copy {
  display: grid;
  flex: 1;
  gap: 4px;
  min-width: 0;
}
.trigger-card-copy b {
  font-size: 14px;
}
.trigger-card-copy small {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}
.trigger-card-radio {
  flex: 0 0 16px;
  width: 16px;
  height: 16px;
  border: 1px solid var(--line);
  border-radius: 50%;
}
.trigger-card.is-selected .trigger-card-radio {
  border: 5px solid var(--blue);
  background: var(--surface);
}
.cron-panel {
  display: grid;
  gap: 10px;
  margin-top: 4px;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--blue-soft);
}
.cron-panel-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.cron-panel-heading b {
  display: flex;
  align-items: center;
  gap: 8px;
}
.cron-panel-heading b :deep(svg) {
  color: var(--blue);
}
.cron-panel-heading > span {
  color: var(--muted);
  font-size: 12px;
}
.cron-panel :deep(.el-form-item) {
  margin-bottom: 0;
}
.cron-panel :deep(.el-form-item__content) {
  display: grid;
  gap: 4px;
}
.cron-panel small {
  color: var(--muted);
}
.cron-preset-row {
  gap: 8px;
}
.cron-preset-row :deep(.el-button) {
  margin: 0;
}
.wizard-source-target {
  display: grid;
  gap: 14px;
}
.wizard-source-target .form-section {
  padding: 15px;
}
.wizard-section-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--line);
}
.wizard-section-title h4 {
  margin: 0 0 4px;
  font-size: 14px;
}
.wizard-section-title p {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}
.unpack-wizard :deep(.select-trigger-field .el-form-item__label) {
  pointer-events: none;
}
.wizard-source-target .source-choice {
  margin-bottom: 12px;
}
.wizard-source-target .form-grid {
  margin-bottom: -4px;
}
.wizard-source-target :deep(.source-seed-field .el-form-item__label) {
  display: flex;
  align-items: center;
  width: auto !important;
  min-width: 0;
  padding-right: 0;
  line-height: 1.35;
}
.source-seed-label-row {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
  min-width: 0;
}
.source-seed-hint {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
  padding: 3px 7px;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--blue-soft);
  color: var(--blue);
  font-size: 11px;
  font-weight: 400;
  line-height: 1.35;
  white-space: normal;
}
.source-seed-hint :deep(svg) {
  flex: 0 0 auto;
}
.wizard-file-filters > .wizard-section-title {
  margin-bottom: 16px;
}
.wizard-file-filters :deep(.extension-filter-item .el-form-item__label) {
  display: flex;
  align-items: center;
  width: auto !important;
  min-width: 0;
  padding-right: 0;
  line-height: 1.35;
}
.extension-label-row {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
  min-width: 0;
}
.extension-label-title {
  font-weight: 650;
}
.extension-label-hint {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
  padding: 3px 7px;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--blue-soft);
  color: var(--blue);
  font-size: 11px;
  font-weight: 400;
  line-height: 1.35;
  white-space: nowrap;
}
.extension-label-hint :deep(svg) {
  flex: 0 0 auto;
}
.wizard-file-filters > .form-grid :deep(.el-form-item__content) {
  min-width: 0;
}
.wizard-file-filters :deep(.el-input-number) {
  width: 100%;
}
.wizard-file-filters > .el-checkbox {
  margin: 0 0 18px;
}
.advanced-rule-block {
  margin-top: 16px;
}
.advanced-toggle {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--canvas);
  color: var(--text);
  text-align: left;
  font: inherit;
  cursor: pointer;
}
.advanced-toggle.is-expanded {
  border-radius: 10px 10px 0 0;
}
.advanced-toggle-icon {
  display: grid;
  flex: 0 0 30px;
  width: 30px;
  height: 30px;
  place-items: center;
  border-radius: 8px;
  background: var(--blue-soft);
  color: var(--blue);
  font-size: 18px;
}
.advanced-toggle-copy {
  display: grid;
  flex: 1;
  gap: 4px;
  min-width: 0;
}
.advanced-toggle-copy b {
  font-size: 13px;
}
.advanced-toggle-copy small {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.45;
}
.advanced-toggle-action {
  flex: 0 0 auto;
  color: var(--blue);
  font-size: 12px;
  font-weight: 650;
}
.advanced-rule-content {
  padding: 14px;
  border: 1px solid var(--line);
  border-top: 0;
  border-radius: 0 0 10px 10px;
}
.advanced-rule-content .form-grid {
  margin-bottom: 0;
}
.advanced-rule-content :deep(.el-form-item) {
  min-width: 0;
}
.advanced-rule-note {
  display: block;
  margin-top: 4px;
}
.wizard-review-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.review-card {
  min-width: 0;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 11px;
  background: var(--surface);
}
.review-card > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: -2px 0 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--line);
}
.review-card > header b {
  font-size: 13px;
}
.review-card > header :deep(.el-button) {
  margin: 0;
}
.review-list {
  display: grid;
  gap: 8px;
  margin: 0;
}
.review-list > div {
  display: grid;
  grid-template-columns: minmax(90px, 0.7fr) minmax(0, 1.5fr);
  gap: 10px;
  align-items: start;
}
.review-list dt {
  color: var(--muted);
  font-size: 12px;
}
.review-list dd {
  min-width: 0;
  margin: 0;
  color: var(--text);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.review-list code {
  color: var(--blue);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.wizard-confirm-note {
  display: flex;
  grid-column: 1 / -1;
  align-items: flex-start;
  gap: 8px;
  padding: 11px 12px;
  border-radius: 8px;
  background: var(--blue-soft);
  color: var(--text);
  font-size: 12px;
  line-height: 1.5;
}
.wizard-confirm-note :deep(svg) {
  flex: 0 0 auto;
  color: var(--blue);
}
.wizard-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
}
.wizard-footer-note {
  color: var(--muted);
  font-size: 12px;
}
.wizard-footer-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.wizard-footer-actions :deep(.el-button) {
  margin: 0;
}
@media (max-width: 900px) {
  .toolbar,
  .table-head,
  .unpack-filter-toolbar,
  .selection-summary {
    align-items: stretch;
    flex-direction: column;
  }
  .form-grid,
  .form-grid.three,
  .metric-grid,
  .pipeline,
  .tree-picker,
  .cron-parts {
    grid-template-columns: 1fr;
  }
  .filters,
  .scan-toolbar {
    display: grid;
    grid-template-columns: 1fr;
    width: 100%;
  }
  .unpack-filter-toolbar .toolbar-actions {
    width: 100%;
    justify-content: flex-end;
  }
  .filters > *,
  .scan-toolbar > * {
    width: 100% !important;
  }
  .inline-field {
    grid-template-columns: 1fr;
  }
}
</style>
