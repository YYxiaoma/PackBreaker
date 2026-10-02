<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';

import { toApiProblem } from '../api/client';
import { browseTaskDirectories, type TaskDirectoryEntry } from '../api/taskDefinitions';
import {
  createMovieDedupJob,
  executeMovieDedupPairs,
  listMovieDedupJobs,
  listMovieDedupPairs,
  precheckMovieDedup,
  startMovieDedupJob,
  type MovieDedupCrossFilesystemPolicy,
  type MovieDedupJob,
  type MovieDedupMode,
  type MovieDedupPair,
  type MovieDedupPrecheck,
} from '../api/movieDedup';

const emit = defineEmits<{
  stats: [value: { total: number; running: number; review: number; completed: number }];
}>();

interface Draft {
  name: string;
  sourceRoot: string;
  targetRoot: string;
  mode: MovieDedupMode;
  crossFilesystemPolicy: MovieDedupCrossFilesystemPolicy;
  includeSubdirectories: boolean;
  minSizeMb: number | null;
  videoExtensions: string;
}

const jobs = ref<MovieDedupJob[]>([]);
const loading = ref(false);
const saving = ref(false);
const starting = ref<Record<string, boolean>>({});
const createVisible = ref(false);
const precheckLoading = ref(false);
const precheckResult = ref<MovieDedupPrecheck | null>(null);
const pairDrawerVisible = ref(false);
const pairLoading = ref(false);
const executing = ref(false);
const activeJob = ref<MovieDedupJob | null>(null);
const pairs = ref<MovieDedupPair[]>([]);
const selectedPairIds = ref<string[]>([]);
const directoryPickerVisible = ref(false);
const directoryPickerTarget = ref<'source' | 'target'>('source');
const directoryPath = ref('.');
const directoryEntries = ref<TaskDirectoryEntry[]>([]);
const directoryLoading = ref(false);
let pollTimer: ReturnType<typeof setInterval> | undefined;

const draft = reactive<Draft>(freshDraft());

const executablePairs = computed(() =>
  pairs.value.filter(
    (item) =>
      ['VERIFIED_DUPLICATE', 'REVIEW_REQUIRED'].includes(item.status) &&
      ['HARDLINK', 'SYMLINK'].includes(item.resolved_action),
  ),
);

const selectedPairs = computed(() =>
  pairs.value.filter((item) => selectedPairIds.value.includes(item.id)),
);

function freshDraft(): Draft {
  return {
    name: '',
    sourceRoot: '',
    targetRoot: '',
    mode: 'AUTO',
    crossFilesystemPolicy: 'STOP',
    includeSubdirectories: true,
    minSizeMb: 0,
    videoExtensions: '.mkv, .mp4, .ts, .m2ts, .avi, .mov, .wmv',
  };
}

function publishStats(): void {
  emit('stats', {
    total: jobs.value.length,
    running: jobs.value.filter((item) => item.status === 'RUNNING').length,
    review: jobs.value.filter((item) => item.status === 'REVIEW_REQUIRED').length,
    completed: jobs.value.filter((item) => ['COMPLETED', 'PARTIAL_FAILED'].includes(item.status))
      .length,
  });
}

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    jobs.value = await listMovieDedupJobs();
    publishStats();
    if (activeJob.value) {
      activeJob.value =
        jobs.value.find((item) => item.id === activeJob.value?.id) ?? activeJob.value;
      if (pairDrawerVisible.value) await loadPairs(activeJob.value.id, false);
    }
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    loading.value = false;
  }
}

function openCreate(): void {
  Object.assign(draft, freshDraft());
  precheckResult.value = null;
  createVisible.value = true;
}

async function runPrecheck(): Promise<MovieDedupPrecheck | null> {
  if (!draft.sourceRoot.trim() || !draft.targetRoot.trim()) {
    ElMessage.warning('请先选择保留目录 A 和去重目录 B');
    return null;
  }
  precheckLoading.value = true;
  try {
    const result = await precheckMovieDedup({
      source_root: draft.sourceRoot.trim(),
      target_root: draft.targetRoot.trim(),
      mode: draft.mode,
      cross_filesystem_policy: draft.crossFilesystemPolicy,
    });
    precheckResult.value = result;
    return result;
  } catch (caught) {
    precheckResult.value = null;
    ElMessage.error(toApiProblem(caught).message);
    return null;
  } finally {
    precheckLoading.value = false;
  }
}

async function createAndStart(): Promise<void> {
  if (!draft.name.trim()) {
    ElMessage.warning('请输入任务名称');
    return;
  }
  const checked = await runPrecheck();
  if (!checked) return;
  if (checked.resolved_action === 'BLOCKED') {
    ElMessage.warning('当前目录无法按所选方式去重，请调整跨文件系统策略或改用软链接/仅扫描');
    return;
  }
  saving.value = true;
  try {
    const extensions = draft.videoExtensions
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean);
    const created = await createMovieDedupJob({
      name: draft.name.trim(),
      source_root: draft.sourceRoot.trim(),
      target_root: draft.targetRoot.trim(),
      mode: draft.mode,
      cross_filesystem_policy: draft.crossFilesystemPolicy,
      include_subdirectories: draft.includeSubdirectories,
      min_size_bytes: Math.max(0, Math.round((draft.minSizeMb ?? 0) * 1024 * 1024)),
      video_extensions: extensions,
    });
    await startMovieDedupJob(created.id);
    createVisible.value = false;
    ElMessage.success('影片去重任务已创建，后台开始扫描与 SHA-256 校验');
    await refresh();
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    saving.value = false;
  }
}

async function startJob(job: MovieDedupJob): Promise<void> {
  starting.value = { ...starting.value, [job.id]: true };
  try {
    await startMovieDedupJob(job.id);
    ElMessage.success('影片去重任务已开始后台扫描');
    await refresh();
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    const next = { ...starting.value };
    delete next[job.id];
    starting.value = next;
  }
}

async function openPairs(job: MovieDedupJob): Promise<void> {
  activeJob.value = job;
  selectedPairIds.value = [];
  pairDrawerVisible.value = true;
  await loadPairs(job.id);
}

async function loadPairs(jobId: string, showError = true): Promise<void> {
  pairLoading.value = true;
  try {
    pairs.value = await listMovieDedupPairs(jobId);
    selectedPairIds.value = selectedPairIds.value.filter((id) =>
      pairs.value.some((item) => item.id === id),
    );
  } catch (caught) {
    if (showError) ElMessage.error(toApiProblem(caught).message);
  } finally {
    pairLoading.value = false;
  }
}

function pairSelectable(row: MovieDedupPair): boolean {
  return (
    ['VERIFIED_DUPLICATE', 'REVIEW_REQUIRED'].includes(row.status) &&
    ['HARDLINK', 'SYMLINK'].includes(row.resolved_action)
  );
}

function onPairSelection(rows: MovieDedupPair[]): void {
  const seenTargets = new Set<string>();
  const unique: string[] = [];
  for (const row of rows) {
    if (seenTargets.has(row.target_relative_path)) continue;
    seenTargets.add(row.target_relative_path);
    unique.push(row.id);
  }
  selectedPairIds.value = unique;
}

async function executeSelected(): Promise<void> {
  const job = activeJob.value;
  if (!job || !selectedPairIds.value.length) {
    ElMessage.warning('请先勾选要执行的重复影片');
    return;
  }
  const selected = selectedPairs.value;
  const reclaim = selected.reduce((total, item) => total + item.estimated_reclaimable_bytes, 0);
  const symlinkCount = selected.filter((item) => item.resolved_action === 'SYMLINK').length;
  const reviewCount = selected.filter((item) => item.status === 'REVIEW_REQUIRED').length;
  const lines = [
    `将处理 ${selected.length} 个 B 目录文件，预计释放 ${formatBytes(reclaim)}。`,
    '执行前会再次校验 A/B 文件身份，并通过 journal + 原子交换替换 B。',
  ];
  if (reviewCount) lines.push(`其中 ${reviewCount} 个为“内容完全一致但元数据需人工确认”的候选。`);
  if (symlinkCount)
    lines.push(`其中 ${symlinkCount} 个会使用软链接；A 路径移动或删除会导致 B 失效。`);
  try {
    await ElMessageBox.confirm(lines.join('\n\n'), '确认执行影片去重', {
      type: 'warning',
      confirmButtonText: '确认执行',
      cancelButtonText: '取消',
    });
  } catch (caught) {
    if (caught === 'cancel' || caught === 'close') return;
    throw caught;
  }
  executing.value = true;
  try {
    const updated = await executeMovieDedupPairs(
      job.id,
      selectedPairIds.value,
      `movie-dedup-${job.id}-${crypto.randomUUID()}`,
    );
    activeJob.value = updated;
    selectedPairIds.value = [];
    ElMessage.success('影片去重执行完成，已通过最终链接校验');
    await Promise.all([refresh(), loadPairs(job.id, false)]);
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
    await loadPairs(job.id, false);
  } finally {
    executing.value = false;
  }
}

async function openDirectoryPicker(target: 'source' | 'target'): Promise<void> {
  directoryPickerTarget.value = target;
  directoryPath.value = target === 'source' ? draft.sourceRoot || '.' : draft.targetRoot || '.';
  directoryPickerVisible.value = true;
  await loadDirectory(directoryPath.value);
}

async function loadDirectory(path: string): Promise<void> {
  directoryLoading.value = true;
  try {
    const result = await browseTaskDirectories(path);
    directoryPath.value = result.current_path;
    directoryEntries.value = result.entries;
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    directoryLoading.value = false;
  }
}

function directoryParent(path: string): string | null {
  if (path === '.') return null;
  const parts = path.split('/');
  parts.pop();
  return parts.length ? parts.join('/') : '.';
}

function selectCurrentDirectory(): void {
  if (directoryPickerTarget.value === 'source') draft.sourceRoot = directoryPath.value;
  else draft.targetRoot = directoryPath.value;
  precheckResult.value = null;
  directoryPickerVisible.value = false;
}

function formatBytes(value: number): string {
  if (value >= 1024 ** 4) return `${(value / 1024 ** 4).toFixed(2)} TB`;
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${Math.max(0, value)} B`;
}

function modeLabel(value: MovieDedupMode | MovieDedupPair['resolved_action']): string {
  if (value === 'AUTO') return '自动选择';
  if (value === 'HARDLINK') return '硬链接';
  if (value === 'SYMLINK') return '软链接';
  if (value === 'SCAN_ONLY') return '仅扫描';
  return '已阻断';
}

function phaseLabel(value: string): string {
  const labels: Record<string, string> = {
    PENDING: '等待开始',
    SCANNING_SOURCE: '扫描保留目录 A',
    SCANNING_TARGET: '扫描去重目录 B',
    MATCHING: '筛选重复候选',
    VERIFYING: '完整 SHA-256 校验',
    REVIEW: '等待审核',
    EXECUTING: '执行安全替换',
    FINAL_VERIFYING: '最终校验',
    COMPLETED: '已完成',
    FAILED: '失败',
  };
  return labels[value] ?? value;
}

function statusType(value: string): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  if (value === 'COMPLETED') return 'success';
  if (value === 'FAILED' || value === 'RECOVERY_REQUIRED') return 'danger';
  if (value === 'REVIEW_REQUIRED' || value === 'PARTIAL_FAILED') return 'warning';
  if (value === 'RUNNING') return 'primary';
  return 'info';
}

function pairStatusLabel(value: string): string {
  const labels: Record<string, string> = {
    CANDIDATE: '待校验',
    VERIFIED_DUPLICATE: '已确认重复',
    REVIEW_REQUIRED: '需人工确认',
    ALREADY_DEDUPLICATED: '已共享 inode',
    COMPLETED: '已去重',
    BLOCKED: '已阻断',
    FAILED: '失败',
  };
  return labels[value] ?? value;
}

function hashLabel(row: MovieDedupPair): string {
  if (row.full_hash_match) return '完整 SHA-256 一致';
  if (row.quick_hash_match) return '快速指纹一致';
  if (row.size_match) return '仅大小一致';
  return '未匹配';
}

function metadataText(metadata: Record<string, string>): string {
  const values = [metadata.year, metadata.resolution, metadata.release_group].filter(Boolean);
  return values.length ? values.join(' · ') : '元数据不足';
}

onMounted(() => {
  void refresh();
  pollTimer = setInterval(() => {
    if (jobs.value.some((item) => item.status === 'RUNNING')) void refresh();
  }, 2000);
});

onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer);
});

defineExpose({ openCreate, refresh });
</script>

<template>
  <div class="movie-dedup-panel">
    <div class="table-card" v-loading="loading">
      <div class="table-card-head">
        <div>
          <b>影片去重</b>
          <span>{{ jobs.length }} 个任务</span>
        </div>
        <small>最终自动替换必须完整 SHA-256 一致；默认优先硬链接，跨文件系统不会静默降级。</small>
      </div>
      <el-empty v-if="!loading && !jobs.length" description="暂无影片去重任务" />
      <el-table v-else :data="jobs">
        <el-table-column label="任务名称" min-width="180">
          <template #default="scope">
            <div class="primary-cell">
              <b>{{ scope.row.name }}</b>
              <small>{{ scope.row.id }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="保留目录 A" min-width="190">
          <template #default="scope"
            ><code>{{ scope.row.source_root }}</code></template
          >
        </el-table-column>
        <el-table-column label="去重目录 B" min-width="190">
          <template #default="scope"
            ><code>{{ scope.row.target_root }}</code></template
          >
        </el-table-column>
        <el-table-column label="方式" width="105">
          <template #default="scope">{{ modeLabel(scope.row.mode) }}</template>
        </el-table-column>
        <el-table-column label="阶段" min-width="150">
          <template #default="scope">
            <div class="primary-cell">
              <b>{{ phaseLabel(scope.row.phase) }}</b>
              <small
                >A {{ scope.row.source_file_count }} · B {{ scope.row.target_file_count }} · 候选
                {{ scope.row.candidate_count }}</small
              >
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="125">
          <template #default="scope">
            <el-tag :type="statusType(scope.row.status)" effect="light">{{
              scope.row.status
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="预计释放" width="120">
          <template #default="scope">{{
            formatBytes(scope.row.estimated_reclaimable_bytes)
          }}</template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="scope">
            <el-button
              v-if="scope.row.status === 'PENDING'"
              link
              type="primary"
              :loading="Boolean(starting[scope.row.id])"
              @click="startJob(scope.row)"
              >开始扫描</el-button
            >
            <el-button link type="primary" @click="openPairs(scope.row)">查看候选</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog
      v-model="createVisible"
      title="新增影片去重任务"
      width="min(820px, 94vw)"
      destroy-on-close
    >
      <div class="create-form">
        <div class="form-section">
          <h3>基本信息</h3>
          <el-form-item label="任务名称" required>
            <el-input v-model="draft.name" maxlength="120" placeholder="例如：电影库 4K 影片去重" />
          </el-form-item>
          <div class="form-grid">
            <el-form-item label="保留目录 A" required>
              <div class="directory-field">
                <el-input
                  v-model="draft.sourceRoot"
                  placeholder="相对于 /data，例如 movies-main"
                  @input="precheckResult = null"
                />
                <el-button @click="openDirectoryPicker('source')">选择目录</el-button>
              </div>
              <small class="hint">A 是权威来源，影片去重不会替换 A。</small>
            </el-form-item>
            <el-form-item label="去重目录 B" required>
              <div class="directory-field">
                <el-input
                  v-model="draft.targetRoot"
                  placeholder="相对于 /data，例如 movies-copy"
                  @input="precheckResult = null"
                />
                <el-button @click="openDirectoryPicker('target')">选择目录</el-button>
              </div>
              <small class="hint">只有 B 中完整 SHA-256 与 A 一致的文件才可能被替换。</small>
            </el-form-item>
          </div>
        </div>
        <div class="form-section">
          <h3>去重策略</h3>
          <div class="form-grid">
            <el-form-item label="去重方式">
              <el-select v-model="draft.mode" @change="precheckResult = null">
                <el-option label="自动选择（默认优先硬链接）" value="AUTO" />
                <el-option label="仅硬链接" value="HARDLINK" />
                <el-option label="仅软链接" value="SYMLINK" />
                <el-option label="仅扫描，不修改文件" value="SCAN_ONLY" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="draft.mode === 'AUTO'" label="跨文件系统">
              <el-select v-model="draft.crossFilesystemPolicy" @change="precheckResult = null">
                <el-option label="停止并提示（默认）" value="STOP" />
                <el-option label="明确允许改用软链接" value="SYMLINK" />
              </el-select>
            </el-form-item>
            <el-form-item label="最小大小（MB）">
              <el-input-number v-model="draft.minSizeMb" :min="0" :controls="false" />
            </el-form-item>
            <el-form-item label="视频扩展名">
              <el-input v-model="draft.videoExtensions" />
            </el-form-item>
          </div>
          <el-checkbox v-model="draft.includeSubdirectories">包含子目录</el-checkbox>
          <el-alert
            v-if="
              draft.mode === 'SYMLINK' ||
              (draft.mode === 'AUTO' && draft.crossFilesystemPolicy === 'SYMLINK')
            "
            type="warning"
            :closable="false"
            title="软链接会依赖 A 的稳定路径；读取 B 的其它容器必须能以兼容挂载访问 A。"
          />
        </div>
        <el-alert
          v-if="precheckResult"
          :type="precheckResult.resolved_action === 'BLOCKED' ? 'error' : 'success'"
          :closable="false"
          :title="`预检查：${precheckResult.same_filesystem ? 'A/B 同一文件系统' : 'A/B 跨文件系统'} · 最终方式 ${modeLabel(precheckResult.resolved_action)}`"
          :description="
            precheckResult.blocked_reasons.join(', ') ||
            `A device ${precheckResult.source_device} · B device ${precheckResult.target_device}`
          "
        />
      </div>
      <template #footer>
        <el-button :loading="precheckLoading" @click="runPrecheck">检查目录</el-button>
        <el-button type="primary" :loading="saving" @click="createAndStart"
          >创建并开始扫描</el-button
        >
      </template>
    </el-dialog>

    <el-dialog v-model="directoryPickerVisible" title="选择目录" width="min(660px, 92vw)">
      <div class="directory-browser" v-loading="directoryLoading">
        <div class="directory-toolbar">
          <el-button
            :disabled="directoryParent(directoryPath) === null"
            @click="loadDirectory(directoryParent(directoryPath) || '.')"
            >返回上级</el-button
          >
          <code>/data/{{ directoryPath === '.' ? '' : directoryPath }}</code>
        </div>
        <el-table :data="directoryEntries" height="360">
          <el-table-column label="目录">
            <template #default="scope">
              <el-button link type="primary" @click="loadDirectory(scope.row.path)">{{
                scope.row.name
              }}</el-button>
            </template>
          </el-table-column>
          <el-table-column prop="path" label="路径" min-width="260" />
        </el-table>
      </div>
      <template #footer>
        <el-button type="primary" @click="selectCurrentDirectory">选择当前目录</el-button>
      </template>
    </el-dialog>

    <el-drawer
      v-model="pairDrawerVisible"
      title="影片去重候选"
      size="min(1180px, 96vw)"
      append-to-body
    >
      <template v-if="activeJob">
        <div class="summary-grid">
          <div>
            <small>任务</small><b>{{ activeJob.name }}</b>
          </div>
          <div>
            <small>阶段</small><b>{{ phaseLabel(activeJob.phase) }}</b>
          </div>
          <div>
            <small>已验证重复</small><b>{{ activeJob.verified_count }}</b>
          </div>
          <div>
            <small>逻辑重复大小</small><b>{{ formatBytes(activeJob.logical_duplicate_bytes) }}</b>
          </div>
          <div>
            <small>预计可释放</small><b>{{ formatBytes(activeJob.estimated_reclaimable_bytes) }}</b>
          </div>
          <div>
            <small>已完成去重</small><b>{{ activeJob.deduplicated_count }}</b>
          </div>
        </div>
        <el-alert
          type="info"
          :closable="false"
          title="大小和快速指纹只用于筛选；只有完整 SHA-256 一致的候选才允许执行。link_count > 1 的旧 B inode 预计释放空间按 0 计算。"
        />
        <div class="pair-actions">
          <span>可执行 {{ executablePairs.length }} 个 · 已选 {{ selectedPairIds.length }} 个</span>
          <el-button :loading="pairLoading" @click="loadPairs(activeJob.id)">刷新候选</el-button>
          <el-button
            type="danger"
            :disabled="!selectedPairIds.length"
            :loading="executing"
            @click="executeSelected"
            >执行去重</el-button
          >
        </div>
        <el-table
          v-loading="pairLoading"
          :data="pairs"
          row-key="id"
          @selection-change="onPairSelection"
        >
          <el-table-column type="selection" width="48" :selectable="pairSelectable" />
          <el-table-column type="expand">
            <template #default="scope">
              <div class="pair-detail">
                <div>
                  <small>A device / inode / nlink</small
                  ><code
                    >{{ scope.row.source_device }} / {{ scope.row.source_inode }} /
                    {{ scope.row.source_link_count }}</code
                  >
                </div>
                <div>
                  <small>B device / inode / nlink</small
                  ><code
                    >{{ scope.row.target_device }} / {{ scope.row.target_inode }} /
                    {{ scope.row.target_link_count }}</code
                  >
                </div>
                <div>
                  <small>A mtime_ns</small><code>{{ scope.row.source_mtime_ns }}</code>
                </div>
                <div>
                  <small>B mtime_ns</small><code>{{ scope.row.target_mtime_ns }}</code>
                </div>
                <div>
                  <small>A SHA-256</small><code>{{ scope.row.source_sha256 || '—' }}</code>
                </div>
                <div>
                  <small>B SHA-256</small><code>{{ scope.row.target_sha256 || '—' }}</code>
                </div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="A 文件" min-width="260">
            <template #default="scope">
              <div class="primary-cell">
                <code class="path-code">{{ scope.row.source_relative_path }}</code>
                <small>{{ metadataText(scope.row.source_media_metadata) }}</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="B 文件" min-width="260">
            <template #default="scope">
              <div class="primary-cell">
                <code class="path-code">{{ scope.row.target_relative_path }}</code>
                <small>{{ metadataText(scope.row.target_media_metadata) }}</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="大小" width="105">
            <template #default="scope">{{ formatBytes(scope.row.target_size_bytes) }}</template>
          </el-table-column>
          <el-table-column label="匹配依据" min-width="155">
            <template #default="scope">
              <div class="primary-cell">
                <b>{{ hashLabel(scope.row) }}</b>
                <small>{{ scope.row.metadata_match ? '元数据一致' : '元数据不足/不一致' }}</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="执行方式" width="100">
            <template #default="scope">{{ modeLabel(scope.row.resolved_action) }}</template>
          </el-table-column>
          <el-table-column label="预计释放" width="105">
            <template #default="scope">{{
              formatBytes(scope.row.estimated_reclaimable_bytes)
            }}</template>
          </el-table-column>
          <el-table-column label="状态" min-width="135">
            <template #default="scope">
              <div class="primary-cell">
                <b>{{ pairStatusLabel(scope.row.status) }}</b>
                <small v-if="scope.row.error_code" class="error-text">{{
                  scope.row.error_code
                }}</small>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.movie-dedup-panel {
  display: grid;
  gap: 16px;
}
.table-card {
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--surface);
  overflow: hidden;
}
.table-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
}
.table-card-head > div {
  display: flex;
  gap: 8px;
  align-items: baseline;
}
.table-card-head span,
.table-card-head small,
.hint,
.primary-cell small,
.summary-grid small,
.pair-detail small {
  color: var(--muted);
  font-size: 11px;
}
.primary-cell {
  display: grid;
  gap: 3px;
}
.create-form {
  display: grid;
  gap: 16px;
  max-height: 68vh;
  overflow: auto;
  padding-right: 6px;
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
.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 18px;
}
.directory-field {
  width: 100%;
  display: flex;
  gap: 8px;
}
.directory-field :deep(.el-input) {
  flex: 1;
}
.directory-toolbar,
.pair-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.directory-toolbar code,
.pair-actions span {
  flex: 1;
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}
.summary-grid > div,
.pair-detail > div {
  display: grid;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 9px;
}
.pair-actions {
  margin: 14px 0;
}
.pair-detail {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  padding: 10px 44px;
}
.pair-detail code,
.path-code {
  overflow-wrap: anywhere;
  white-space: normal;
}
.error-text {
  color: var(--danger) !important;
}
:deep(.el-form-item__label) {
  min-width: 110px;
}
:deep(.el-select),
:deep(.el-input-number) {
  width: 100%;
}
@media (max-width: 860px) {
  .form-grid,
  .summary-grid,
  .pair-detail {
    grid-template-columns: 1fr;
  }
  .table-card-head,
  .pair-actions {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
