<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import {
  CheckCircle2,
  Clock3,
  Copy,
  Eye,
  FolderOpen,
  FolderSearch,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
} from '@lucide/vue';
import { ElMessage } from 'element-plus';

import { toApiProblem } from '../api/client';
import { listDownloaders, type Downloader } from '../api/downloaders';
import { listSites, type Site } from '../api/sites';
import {
  browseUnpackTree,
  createUnpackDefinition,
  createUnpackSourceScan,
  getUnpackExecution,
  getUnpackSourceScanSelectionSummary,
  listUnpackDefinitions,
  listUnpackExecutionItems,
  listUnpackExecutions,
  listUnpackItemCandidates,
  listUnpackSourceScanItems,
  listUnpackTreeRoots,
  retryUnpackItemMatch,
  reviewUnpackItem,
  runUnpackDefinition,
  updateUnpackSourceScanSelection,
  type UnpackDefinition,
  type UnpackDefinitionCreate,
  type UnpackExecution,
  type UnpackExecutionItem,
  type UnpackItemStatus,
  type UnpackMatchCandidate,
  type UnpackMatchCandidateList,
  type UnpackSourceScanItem,
  type UnpackTreeEntry,
} from '../api/unpack';
import { createClientNonce } from '../clientNonce';
import {
  unpackDefinitionStatusLabel,
  unpackExecutionStatusLabel,
  unpackItemStatusLabel,
  unpackProgressPercent,
  unpackSourceKindLabel,
  unpackTriggerLabel,
  unpackVerificationLevelLabel,
} from '../unpackPresentation';
import MovieDedupPanel from './MovieDedupPanel.vue';

type ActiveView = 'UNPACK' | 'DEDUP';
type TriggerKind = 'MANUAL' | 'MONITOR';
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
  sourceKind: SourceKind;
  siteIds: string[];
  sourceDirectory: string;
  sourceToken: string;
  sourceDownloaderId: string;
  downloaderNameContains: string;
  downloaderCategories: string;
  downloaderTags: string;
  outputDirectory: string;
  targetDownloaderId: string;
  extensions: string;
  minSizeMb: number | null;
  maxSizeMb: number | null;
  includeName: string;
  excludeNames: string;
  includeSubdirectories: boolean;
  storageMode: StorageMode;
  retryEnabled: boolean;
  maxRetries: number;
  autoMatchPercent: number;
  cronExpression: string;
}

const activeView = ref<ActiveView>('UNPACK');
const loading = ref(false);
const saving = ref(false);
const definitions = ref<UnpackDefinition[]>([]);
const executions = ref<UnpackExecution[]>([]);
const sites = ref<Site[]>([]);
const downloaders = ref<Downloader[]>([]);
const dedupStats = ref({ total: 0, running: 0, review: 0, completed: 0 });

const typeFilter = ref('');
const statusFilter = ref('');
const keyword = ref('');

const createVisible = ref(false);
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

const executionVisible = ref(false);
const activeExecution = ref<UnpackExecution | null>(null);
const activeDefinition = ref<UnpackDefinition | null>(null);
const executionItems = ref<UnpackExecutionItem[]>([]);
const executionLoading = ref(false);
const itemCursor = ref<string | undefined>();
const itemNextCursor = ref<string | null>(null);
const itemCursorHistory = ref<Array<string | undefined>>([]);

const reviewVisible = ref(false);
const reviewLoading = ref(false);
const reviewSaving = ref(false);
const reviewItem = ref<UnpackExecutionItem | null>(null);
const reviewCandidates = ref<UnpackMatchCandidateList | null>(null);
const selectedCandidateId = ref('');

const cronMinute = ref('*/10');
const cronHour = ref('*');
const cronDay = ref('*');
const cronMonth = ref('*');
const cronWeekday = ref('*');

const draft = reactive<Draft>(freshDraft());

const enabledSites = computed(() => sites.value.filter((item) => item.enabled));
const enabledDownloaders = computed(() => downloaders.value.filter((item) => item.enabled));

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
  const query = keyword.value.trim().toLocaleLowerCase();
  return rows.value.filter((row) => {
    if (typeFilter.value && row.definition.trigger_kind !== typeFilter.value) return false;
    const state = row.execution?.status ?? row.definition.status;
    if (statusFilter.value && state !== statusFilter.value) return false;
    if (!query) return true;
    return (
      row.definition.name.toLocaleLowerCase().includes(query) ||
      sourceText(row.definition).toLocaleLowerCase().includes(query)
    );
  });
});

const unpackStats = computed(() => {
  let manual = 0;
  let monitor = 0;
  let review = 0;
  let errors = 0;
  for (const row of rows.value) {
    if (row.definition.trigger_kind === 'MANUAL') manual += 1;
    else monitor += 1;
    if (row.execution?.status === 'REVIEW_REQUIRED') review += 1;
    if (
      row.definition.status === 'ERROR' ||
      ['COMPLETED_WITH_ERRORS', 'FAILED'].includes(row.execution?.status ?? '')
    ) {
      errors += 1;
    }
  }
  return { total: rows.value.length, manual, monitor, review, errors };
});

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
    sourceKind: 'DIRECTORY',
    siteIds: [],
    sourceDirectory: '',
    sourceToken: '',
    sourceDownloaderId: '',
    downloaderNameContains: '',
    downloaderCategories: '',
    downloaderTags: '',
    outputDirectory: '',
    targetDownloaderId: '',
    extensions: '.mkv, .mp4, .ts, .m2ts',
    minSizeMb: 0,
    maxSizeMb: null,
    includeName: '',
    excludeNames: 'sample, trailer',
    includeSubdirectories: true,
    storageMode: 'HARDLINK',
    retryEnabled: true,
    maxRetries: 3,
    autoMatchPercent: 100,
    cronExpression: '*/10 * * * *',
  };
}

function resetDraft(): void {
  Object.assign(draft, freshDraft());
  cronMinute.value = '*/10';
  cronHour.value = '*';
  cronDay.value = '*';
  cronMonth.value = '*';
  cronWeekday.value = '*';
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
  if (['MATCH_TIMEOUT', 'MATCH_ERROR', 'CONTENT_MISMATCH', 'EXECUTION_ERROR'].includes(status)) {
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
    '自动 ' +
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

function progressText(execution: UnpackExecution | null): string {
  if (!execution || execution.total_count <= 0) return '—';
  const progressed = Math.min(
    execution.total_count,
    execution.completed_count +
      execution.content_verified_count +
      execution.content_mismatch_count +
      execution.review_count +
      execution.timeout_count +
      execution.error_count,
  );
  return progressed + ' / ' + execution.total_count;
}

function progressPercent(execution: UnpackExecution | null): number {
  if (!execution) return 0;
  return unpackProgressPercent(execution.total_count, execution.completed_count);
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
    const [nextDefinitions, nextExecutions, nextSites, nextDownloaders] = await Promise.all([
      listUnpackDefinitions(),
      listUnpackExecutions({ limit: 500 }),
      listSites(),
      listDownloaders(),
    ]);
    definitions.value = nextDefinitions;
    executions.value = nextExecutions;
    sites.value = nextSites;
    downloaders.value = nextDownloaders;
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    loading.value = false;
  }
}

function openCreate(): void {
  resetDraft();
  createVisible.value = true;
}

function chooseTrigger(kind: TriggerKind): void {
  draft.triggerKind = kind;
  if (kind === 'MANUAL') {
    draft.sourceKind = 'DIRECTORY';
  }
}

function chooseSource(kind: SourceKind): void {
  if (draft.triggerKind === 'MANUAL' && kind === 'DOWNLOADER') return;
  draft.sourceKind = kind;
  if (kind === 'DOWNLOADER' && draft.sourceDownloaderId && !draft.targetDownloaderId) {
    draft.targetDownloaderId = draft.sourceDownloaderId;
  }
}

function onSourceDownloaderChange(value: string): void {
  if (draft.sourceKind === 'DOWNLOADER' && !draft.targetDownloaderId) {
    draft.targetDownloaderId = value;
  }
}

function applyCronPreset(value: string): void {
  draft.cronExpression = value;
  const parts = value.split(/\s+/);
  if (parts.length === 5) {
    cronMinute.value = parts[0] ?? '*';
    cronHour.value = parts[1] ?? '*';
    cronDay.value = parts[2] ?? '*';
    cronMonth.value = parts[3] ?? '*';
    cronWeekday.value = parts[4] ?? '*';
  }
}

function rebuildCron(): void {
  draft.cronExpression = [
    cronMinute.value,
    cronHour.value,
    cronDay.value,
    cronMonth.value,
    cronWeekday.value,
  ].join(' ');
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
  if (!draft.siteIds.length) {
    ElMessage.warning('至少选择一个扫描站点');
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
  if (draft.triggerKind === 'MONITOR' && !draft.cronExpression.trim()) {
    ElMessage.warning('请输入或通过 Cron 辅助生成执行时间');
    return false;
  }
  return true;
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
        };
  return {
    name: draft.name.trim(),
    trigger_kind: draft.triggerKind,
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
    },
    retry_enabled: draft.retryEnabled,
    max_retries: draft.maxRetries,
    auto_match_threshold_bps: Math.round(Math.min(100, Math.max(0, draft.autoMatchPercent)) * 100),
    cron_expression: draft.triggerKind === 'MONITOR' ? draft.cronExpression.trim() : null,
    timezone: null,
    source_scan_id: selectedScanId ?? null,
  };
}

async function saveDraft(): Promise<void> {
  if (!validateDraft()) return;
  if (draft.triggerKind === 'MANUAL' && draft.sourceKind === 'DIRECTORY') {
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
    await createUnpackDefinition(createPayload(scope, selectedScanId));
    createVisible.value = false;
    scopeConfirmVisible.value = false;
    mediaSelectVisible.value = false;
    ElMessage.success('任务已保存，当前状态为“待执行”');
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
  await loadExecutionItems();
  executionVisible.value = true;
}

async function loadExecutionItems(cursor?: string): Promise<void> {
  if (!activeExecution.value) return;
  executionLoading.value = true;
  try {
    const result = await listUnpackExecutionItems(activeExecution.value.id, {
      cursor,
      limit: 20,
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
}

async function previousItemPage(): Promise<void> {
  if (!itemCursorHistory.value.length) return;
  const previous = itemCursorHistory.value.pop();
  await loadExecutionItems(previous);
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
  return item.status === 'MATCH_TIMEOUT' || item.status === 'MATCH_ERROR';
}

async function retryItem(item: UnpackExecutionItem): Promise<void> {
  try {
    await retryUnpackItemMatch(item.id, item.version);
    ElMessage.success('已重新进入匹配队列');
    await loadExecutionItems(itemCursor.value);
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
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

function candidateEvidence(candidate: UnpackMatchCandidate): string[] {
  const evidence = asRecord(candidate.evidence);
  const result: string[] = [];
  const exact = asRecord(evidence.exact);
  const exactLabels: Record<string, string> = {
    title: '标题一致',
    external_id: '外部 ID 一致',
    year: '年份一致',
    episode: '季集一致',
    resolution: '分辨率一致',
    release_source: '来源一致',
    codec: '编码一致',
    release_group: '制作组一致',
    size: '大小一致',
  };
  for (const [key, label] of Object.entries(exactLabels)) {
    if (exact[key] === true) result.push(label);
  }
  const conflicts = evidence.hard_conflicts;
  if (Array.isArray(conflicts)) {
    for (const item of conflicts) {
      if (typeof item === 'string') result.push(hardConflictText(item));
    }
  }
  return result.slice(0, 8);
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
});
</script>

<template>
  <section class="v2-task-center">
    <div class="toolbar">
      <div>
        <h2>任务中心</h2>
        <p>数据拆包统一使用影片发现、候选匹配、内容校验与安全辅种执行链。</p>
      </div>
      <div class="toolbar-actions">
        <el-button :loading="loading" @click="refresh"><RefreshCw :size="15" />刷新</el-button>
        <el-button v-if="activeView === 'UNPACK'" type="primary" @click="openCreate">
          <Plus :size="15" />新增任务
        </el-button>
      </div>
    </div>

    <div class="mode-cards">
      <button
        :class="['mode-card', { active: activeView === 'UNPACK' }]"
        @click="activeView = 'UNPACK'"
      >
        <span class="mode-icon"><FolderSearch :size="22" /></span>
        <span>
          <b>数据拆包</b>
          <small>
            手动 {{ unpackStats.manual }} · 监控 {{ unpackStats.monitor }} · 待人工审核
            {{ unpackStats.review }} · 异常 {{ unpackStats.errors }}
          </small>
        </span>
        <strong>{{ unpackStats.total }}</strong>
      </button>
      <button
        :class="['mode-card', { active: activeView === 'DEDUP' }]"
        @click="activeView = 'DEDUP'"
      >
        <span class="mode-icon dedup"><Copy :size="22" /></span>
        <span>
          <b>数据去重</b>
          <small>
            运行中 {{ dedupStats.running }} · 待审核 {{ dedupStats.review }} · 已完成
            {{ dedupStats.completed }}
          </small>
        </span>
        <strong>{{ dedupStats.total }}</strong>
      </button>
    </div>

    <div v-if="activeView === 'UNPACK'" class="table-card" v-loading="loading">
      <div class="table-head">
        <div><b>数据拆包任务</b><span>手动与监控共用同一执行链</span></div>
        <div class="filters">
          <el-select v-model="typeFilter" clearable placeholder="全部类型" style="width: 130px">
            <el-option label="手动拆包" value="MANUAL" />
            <el-option label="监控拆包" value="MONITOR" />
          </el-select>
          <el-select v-model="statusFilter" clearable placeholder="全部状态" style="width: 150px">
            <el-option label="待执行" value="PENDING_EXECUTION" />
            <el-option label="匹配中" value="MATCHING" />
            <el-option label="待人工审核" value="REVIEW_REQUIRED" />
            <el-option label="内容校验中" value="CONTENT_VERIFYING" />
            <el-option label="执行中" value="EXECUTING" />
            <el-option label="下载器校验中" value="CLIENT_VERIFYING" />
            <el-option label="已完成" value="COMPLETED" />
            <el-option label="完成但有异常" value="COMPLETED_WITH_ERRORS" />
          </el-select>
          <el-input
            v-model="keyword"
            clearable
            placeholder="搜索任务名称 / 来源"
            style="width: 230px"
          >
            <template #prefix><Search :size="14" /></template>
          </el-input>
        </div>
      </div>
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
            unpackTriggerLabel(row.definition.trigger_kind)
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
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="!row.execution && row.definition.status === 'PENDING_EXECUTION'"
              link
              type="success"
              @click="runDefinition(row)"
            >
              <Play :size="14" />执行
            </el-button>
            <el-button v-if="row.execution" link type="primary" @click="openExecution(row)">
              <Eye :size="14" />查看
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="table-note">
        保存任务只创建“待执行”任务，不会自动启动；达到自动匹配阈值的候选仍必须通过种子内容校验后才能辅种。
      </div>
    </div>

    <MovieDedupPanel v-else @stats="dedupStats = $event" />

    <el-dialog
      v-model="createVisible"
      title="新增数据拆包任务"
      width="min(980px, 96vw)"
      destroy-on-close
    >
      <div class="form-sections">
        <section class="form-section">
          <h3>1. 基本信息</h3>
          <div class="form-grid">
            <el-form-item label="任务名称"
              ><el-input v-model="draft.name" placeholder="请输入任务名称"
            /></el-form-item>
            <el-form-item label="任务类型">
              <div class="choice-row">
                <el-button
                  :type="draft.triggerKind === 'MANUAL' ? 'primary' : 'default'"
                  @click="chooseTrigger('MANUAL')"
                  >手动拆包</el-button
                >
                <el-button
                  :type="draft.triggerKind === 'MONITOR' ? 'primary' : 'default'"
                  @click="chooseTrigger('MONITOR')"
                  >监控拆包</el-button
                >
              </div>
            </el-form-item>
            <el-form-item label="扫描站点">
              <el-select
                v-model="draft.siteIds"
                multiple
                collapse-tags
                collapse-tags-tooltip
                placeholder="选择站点"
              >
                <el-option
                  v-for="site in enabledSites"
                  :key="site.id"
                  :label="site.name"
                  :value="site.id"
                />
              </el-select>
            </el-form-item>
            <el-form-item v-if="draft.triggerKind === 'MONITOR'" label="执行时间">
              <el-input v-model="draft.cronExpression" placeholder="*/10 * * * *" />
            </el-form-item>
          </div>
          <div v-if="draft.triggerKind === 'MONITOR'" class="cron-helper">
            <b>Cron 辅助</b>
            <div class="preset-row">
              <el-button @click="applyCronPreset('*/5 * * * *')">每 5 分钟</el-button>
              <el-button @click="applyCronPreset('*/10 * * * *')">每 10 分钟</el-button>
              <el-button @click="applyCronPreset('0 * * * *')">每小时</el-button>
              <el-button @click="applyCronPreset('0 3 * * *')">每天 03:00</el-button>
              <el-button @click="applyCronPreset('0 3 * * 1')">每周一 03:00</el-button>
            </div>
            <div class="cron-parts">
              <el-form-item label="分钟"
                ><el-select v-model="cronMinute" @change="rebuildCron"
                  ><el-option
                    v-for="value in ['*', '*/5', '*/10', '0', '30']"
                    :key="value"
                    :label="value"
                    :value="value" /></el-select
              ></el-form-item>
              <el-form-item label="小时"
                ><el-select v-model="cronHour" @change="rebuildCron"
                  ><el-option
                    v-for="value in ['*', '0', '3', '12']"
                    :key="value"
                    :label="value"
                    :value="value" /></el-select
              ></el-form-item>
              <el-form-item label="日期"
                ><el-select v-model="cronDay" @change="rebuildCron"
                  ><el-option
                    v-for="value in ['*', '1', '15']"
                    :key="value"
                    :label="value"
                    :value="value" /></el-select
              ></el-form-item>
              <el-form-item label="月份"
                ><el-select v-model="cronMonth" @change="rebuildCron"
                  ><el-option
                    v-for="value in ['*', '1', '6', '12']"
                    :key="value"
                    :label="value"
                    :value="value" /></el-select
              ></el-form-item>
              <el-form-item label="星期"
                ><el-select v-model="cronWeekday" @change="rebuildCron"
                  ><el-option
                    v-for="value in ['*', '1', '1-5', '0,6']"
                    :key="value"
                    :label="value"
                    :value="value" /></el-select
              ></el-form-item>
            </div>
            <small>点击预设或分钟 / 小时 / 日期 / 月份 / 星期组件，会自动回填 Cron 表达式。</small>
          </div>
        </section>

        <section class="form-section">
          <h3>2. 来源</h3>
          <div class="choice-row source-choice">
            <el-button
              :type="draft.sourceKind === 'DIRECTORY' ? 'primary' : 'default'"
              @click="chooseSource('DIRECTORY')"
              >目录</el-button
            >
            <el-button
              v-if="draft.triggerKind === 'MONITOR'"
              :type="draft.sourceKind === 'DOWNLOADER' ? 'primary' : 'default'"
              @click="chooseSource('DOWNLOADER')"
              >下载器</el-button
            >
          </div>
          <div class="form-grid">
            <template v-if="draft.sourceKind === 'DIRECTORY'">
              <el-form-item label="来源目录">
                <div class="inline-field">
                  <el-input
                    v-model="draft.sourceDirectory"
                    readonly
                    placeholder="选择目录"
                  /><el-button @click="openDirectoryPicker('SOURCE')"
                    ><FolderOpen :size="14" />选择目录</el-button
                  >
                </div>
              </el-form-item>
            </template>
            <template v-else>
              <el-form-item label="来源下载器">
                <el-select
                  v-model="draft.sourceDownloaderId"
                  placeholder="选择下载器"
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
              <el-form-item label="任务名称包含"
                ><el-input v-model="draft.downloaderNameContains" placeholder="例如：Movie / 电影"
              /></el-form-item>
              <el-form-item label="分类"
                ><el-input v-model="draft.downloaderCategories" placeholder="多个分类使用逗号分隔"
              /></el-form-item>
              <el-form-item label="标签"
                ><el-input v-model="draft.downloaderTags" placeholder="多个标签使用逗号分隔"
              /></el-form-item>
            </template>
            <el-form-item label="输出目录">
              <div class="inline-field">
                <el-input
                  v-model="draft.outputDirectory"
                  readonly
                  placeholder="选择目录"
                /><el-button @click="openDirectoryPicker('OUTPUT')"
                  ><FolderOpen :size="14" />选择目录</el-button
                >
              </div>
            </el-form-item>
            <el-form-item label="目标下载器">
              <el-select v-model="draft.targetDownloaderId" placeholder="选择最终辅种下载器">
                <el-option
                  v-for="item in enabledDownloaders"
                  :key="item.id"
                  :label="item.name"
                  :value="item.id"
                />
              </el-select>
            </el-form-item>
          </div>
        </section>

        <section class="form-section">
          <h3>3. 文件过滤</h3>
          <div class="form-grid">
            <el-form-item label="文件类型"
              ><el-input model-value="影视文件" disabled
            /></el-form-item>
            <el-form-item label="后缀名白名单"
              ><el-input v-model="draft.extensions" placeholder=".mkv, .mp4, .ts"
            /></el-form-item>
            <el-form-item label="最小大小（MB）"
              ><el-input-number v-model="draft.minSizeMb" :min="0" controls-position="right"
            /></el-form-item>
            <el-form-item label="最大大小（MB）"
              ><el-input-number
                v-model="draft.maxSizeMb"
                :min="0"
                controls-position="right"
                placeholder="不限"
            /></el-form-item>
            <el-form-item label="包含名称"
              ><el-input v-model="draft.includeName" placeholder="留空表示不限"
            /></el-form-item>
            <el-form-item label="排除名称"
              ><el-input v-model="draft.excludeNames" placeholder="sample, trailer"
            /></el-form-item>
          </div>
          <el-checkbox v-model="draft.includeSubdirectories">包含子目录</el-checkbox>
        </section>

        <section class="form-section">
          <h3>4. 输出与文件冲突</h3>
          <div class="form-grid">
            <el-form-item label="存放方式">
              <el-select v-model="draft.storageMode">
                <el-option label="硬链接" value="HARDLINK" />
                <el-option label="软链接" value="SYMLINK" />
                <el-option label="复制" value="COPY" />
              </el-select>
            </el-form-item>
            <el-form-item label="文件冲突">
              <el-select model-value="VERIFY_REUSE_OR_STOP" disabled>
                <el-option label="校验一致后复用，否则停止" value="VERIFY_REUSE_OR_STOP" />
              </el-select>
            </el-form-item>
          </div>
        </section>

        <section class="form-section advanced">
          <h3>5. 高级执行规则</h3>
          <div class="form-grid three">
            <el-form-item label="自动重试"
              ><el-switch v-model="draft.retryEnabled" active-text="开启" inactive-text="关闭"
            /></el-form-item>
            <el-form-item label="自动重试次数"
              ><el-input-number v-model="draft.maxRetries" :min="0" :max="10"
            /></el-form-item>
            <el-form-item label="自动匹配阈值（%）"
              ><el-input-number
                v-model="draft.autoMatchPercent"
                :min="0"
                :max="100"
                :step="0.1"
                :precision="1"
            /></el-form-item>
          </div>
          <small
            >候选匹配度达到阈值时可自动选择，但仍必须通过种子内容校验，不能仅凭匹配分数直接辅种。</small
          >
        </section>
      </div>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveDraft">保存</el-button>
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

    <el-dialog v-model="directoryPickerVisible" title="选择目录" width="min(850px, 95vw)">
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
      <template #footer>
        <el-button @click="directoryPickerVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmDirectorySelection">使用此目录</el-button>
      </template>
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
                : activeExecution.status === 'REVIEW_REQUIRED'
                  ? 'warning'
                  : 'primary'
            "
            >{{ unpackExecutionStatusLabel(activeExecution.status) }}</el-tag
          ><span
            >完成 {{ activeExecution.completed_count }} / {{ activeExecution.total_count }}</span
          >
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
                unpackItemStatusLabel(row.status)
              }}</el-tag></template
            ></el-table-column
          >
          <el-table-column label="错误说明" min-width="210" show-overflow-tooltip
            ><template #default="{ row }">{{
              row.last_error_message || '—'
            }}</template></el-table-column
          >
          <el-table-column label="操作" width="145" fixed="right">
            <template #default="{ row }">
              <el-button v-if="itemCanReview(row)" link type="primary" @click="openReview(row)"
                >审核</el-button
              >
              <el-button v-if="itemCanRetry(row)" link type="danger" @click="retryItem(row)"
                ><RotateCcw :size="14" />重试</el-button
              >
            </template>
          </el-table-column>
        </el-table>
        <div class="pager">
          <el-button :disabled="!itemCursorHistory.length" @click="previousItemPage"
            >上一页</el-button
          ><el-button :disabled="!itemNextCursor" @click="nextItemPage">下一页</el-button>
        </div>
      </div>
      <template #footer><el-button @click="executionVisible = false">关闭</el-button></template>
    </el-dialog>

    <el-dialog
      v-model="reviewVisible"
      :title="'人工审核候选 · ' + (reviewItem ? mediaTitle(reviewItem) : '')"
      width="min(1040px, 96vw)"
    >
      <div v-loading="reviewLoading">
        <div v-if="reviewItem" class="source-box">
          <strong>{{ mediaTitle(reviewItem) }}</strong>
          <div class="source-meta">
            <span v-for="item in reviewSourceMeta()" :key="item">{{ item }}</span>
          </div>
        </div>
        <div class="review-heading">
          <div>
            <b>候选列表</b><small>默认选中匹配度最高的一条，但必须点击确认后才会继续。</small>
          </div>
          <el-tag type="warning">人工审核</el-tag>
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
              <b>{{ candidate.title }}</b>
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
              <small>{{ candidateVerificationText(candidate) }}</small>
            </div>
            <div class="score">
              <b>{{ (candidate.score_bps / 100).toFixed(1) }}%</b
              ><small>{{
                candidate.is_exact_match
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
        <el-button @click="reviewVisible = false">稍后处理</el-button>
        <el-button
          type="primary"
          :loading="reviewSaving"
          :disabled="!selectedCandidateId"
          @click="submitReview('APPROVE')"
          ><CheckCircle2 :size="14" />确认所选候选并继续</el-button
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
.mode-cards {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
.mode-card {
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 12px;
  padding: 18px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface);
  color: inherit;
  text-align: left;
  transition: 0.16s ease;
}
.mode-card:hover {
  border-color: var(--blue);
  box-shadow: 0 8px 26px rgba(31, 93, 255, 0.08);
}
.mode-card.active {
  border-color: var(--blue);
  background: var(--blue-soft);
  box-shadow: 0 0 0 2px rgba(31, 93, 255, 0.12);
}
.mode-icon {
  display: grid;
  place-items: center;
  width: 42px;
  height: 42px;
  border-radius: 11px;
  background: var(--blue-soft);
  color: var(--blue);
}
.mode-card.active .mode-icon {
  background: var(--blue);
  color: #fff;
}
.mode-icon.dedup {
  color: #7c3aed;
}
.mode-card.active .mode-icon.dedup {
  background: #7c3aed;
  color: #fff;
}
.mode-card span:nth-child(2) {
  display: grid;
  gap: 4px;
}
.mode-card small {
  color: var(--muted);
}
.mode-card strong {
  font-size: 25px;
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
}
.table-head > div:first-child {
  display: flex;
  gap: 8px;
  align-items: baseline;
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
.review-heading > div {
  display: grid;
  gap: 4px;
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
.candidate-main > b {
  display: block;
  margin-bottom: 5px;
  overflow-wrap: anywhere;
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
.candidate-main > small {
  display: block;
  margin-top: 7px;
  color: var(--muted);
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
@media (max-width: 900px) {
  .toolbar,
  .table-head,
  .selection-summary {
    align-items: stretch;
    flex-direction: column;
  }
  .mode-cards,
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
  .filters > *,
  .scan-toolbar > * {
    width: 100% !important;
  }
  .inline-field {
    grid-template-columns: 1fr;
  }
}
</style>
