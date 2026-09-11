<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';

import { ApiProblem } from '../api/client';
import { listDownloaders, type Downloader } from '../api/downloaders';
import {
  cancelTask,
  createTaskUnitExecutionPlan,
  executeTask,
  getTaskUnitExecutionGate,
  getTaskUnitExecutionPlan,
  getTaskUnitReviewVerification,
  getTaskUnitDecision,
  listTaskUnits,
  reverifyTaskUnitDecision,
  refreshTaskUnitExecutionGate,
  submitTaskUnitDecision,
  type ExecutionGate,
  type ExecutionPlan,
  type ReviewVerification,
  type TaskCandidate,
  type TaskEvent,
  type TaskMutationAction,
  type TaskReview,
  type TaskUnit,
} from '../api/tasks';
import type { PreflightReviewItem } from '../preflightReviews';
import {
  cancellationIsInProgress,
  cancellationOptionsAreConsistent,
  canStartTaskCancellation,
  createTaskActionIdempotencyKey,
  formatByteUpperBound,
} from '../taskActionSafety';
import TaskEventTimeline from './TaskEventTimeline.vue';

const props = withDefaults(defineProps<{ item: PreflightReviewItem; live?: boolean }>(), {
  live: true,
});
const emit = defineEmits<{ saved: []; event: [event: TaskEvent] }>();

const unit = ref<TaskUnit | null>(null);
const decision = ref<TaskReview | null>(null);
const verification = ref<ReviewVerification | null>(null);
const executionGate = ref<ExecutionGate | null>(null);
const executionPlan = ref<ExecutionPlan | null>(null);
const targetDownloaders = ref<Downloader[]>([]);
const targetDownloaderId = ref('');
const approvedCandidateId = ref('');
const rejectedCandidateIds = ref<string[]>([]);
const note = ref('');
const manualSources = reactive<Record<string, string>>({});
const loading = ref(false);
const saving = ref(false);
const reverifying = ref(false);
const checkingGate = ref(false);
const planning = ref(false);
const executing = ref(false);
const cancelling = ref(false);
const targetRoot = ref('');
const executeIdempotencyKey = ref('');
const executeReplayPlanId = ref('');
const executeResultUnknown = ref(false);
const cancelIdempotencyKey = ref('');
const removeDownloaderTask = ref(false);
const rollbackCreatedResources = ref(false);
const cancellationAcknowledged = ref(false);
const lastMutation = ref<TaskMutationAction | null>(null);

const eligibleCandidates = computed(() => props.item.candidates.filter((item) => !item.rejected));
const approvedCandidate = computed(
  () => props.item.candidates.find((item) => item.id === approvedCandidateId.value) ?? null,
);
const ambiguousTorrentPaths = computed(() => ambiguousPaths(approvedCandidate.value));
const stateAllowsReview = computed(() =>
  ['PREFLIGHT', 'AWAITING_CONFIRMATION'].includes(props.item.task.status),
);
const canSubmit = computed(
  () =>
    props.item.preflight.current &&
    stateAllowsReview.value &&
    unit.value !== null &&
    !loading.value,
);
const canReverify = computed(
  () =>
    props.item.preflight.current &&
    stateAllowsReview.value &&
    unit.value !== null &&
    decision.value?.requires_reverification === true &&
    decision.value.approved_candidate_id !== null &&
    !loading.value &&
    !reverifying.value,
);
const canPlan = computed(
  () =>
    executionGate.value?.current === true &&
    executionGate.value.eligible === true &&
    props.item.task.status === 'AWAITING_CONFIRMATION' &&
    targetDownloaders.value.some((item) => item.id === targetDownloaderId.value) &&
    targetRoot.value.trim().length > 0 &&
    !planning.value,
);
const selectedTargetDownloader = computed(
  () =>
    targetDownloaders.value.find((item) => item.id === executionPlan.value?.target_downloader_id) ??
    null,
);
const canStartExecute = computed(
  () =>
    executionPlan.value?.ready === true &&
    executionPlan.value.current === true &&
    props.item.task.status === 'AWAITING_CONFIRMATION' &&
    selectedTargetDownloader.value !== null &&
    !loading.value &&
    !executing.value,
);
const canReplayExecute = computed(
  () =>
    executeResultUnknown.value &&
    executeReplayPlanId.value.length > 0 &&
    executeIdempotencyKey.value.length > 0 &&
    !loading.value &&
    !executing.value,
);
const canExecute = computed(() => canStartExecute.value || canReplayExecute.value);
const canStartCancellation = computed(() => canStartTaskCancellation(props.item.task.status));
const cancellationInProgress = computed(() => cancellationIsInProgress(props.item.task.status));
const showCancellationPanel = computed(
  () => canStartCancellation.value || cancellationInProgress.value,
);
const cancellationOptionsConsistent = computed(() =>
  cancellationOptionsAreConsistent(removeDownloaderTask.value, rollbackCreatedResources.value),
);
const canCancel = computed(
  () =>
    canStartCancellation.value &&
    cancellationOptionsConsistent.value &&
    cancellationAcknowledged.value &&
    !loading.value &&
    !cancelling.value,
);

watch(
  () => [props.item.task.id, props.item.task.version] as const,
  (current, previous) => {
    if (!previous || current[0] !== previous[0]) resetActionState();
    void load();
  },
  { immediate: true },
);

watch(approvedCandidateId, (value) => {
  rejectedCandidateIds.value = rejectedCandidateIds.value.filter(
    (candidateId) => candidateId !== value,
  );
  for (const key of Object.keys(manualSources)) {
    if (!ambiguousTorrentPaths.value.includes(key)) delete manualSources[key];
  }
});

watch([removeDownloaderTask, rollbackCreatedResources], () => {
  cancelIdempotencyKey.value = '';
  cancellationAcknowledged.value = false;
});

async function load(): Promise<void> {
  loading.value = true;
  resetForm();
  try {
    const [units, downloaders] = await Promise.all([
      listTaskUnits(props.item.task.id),
      listDownloaders(),
    ]);
    targetDownloaders.value = downloaders.filter(
      (item) =>
        item.type === 'QBITTORRENT' &&
        item.enabled &&
        item.connection_status === 'OK' &&
        item.path_mapping_status === 'OK',
    );
    const inventoryDigest = preflightInventoryDigest(props.item);
    unit.value =
      units.find(
        (item) =>
          item.normalized_unit_key === props.item.task.normalized_unit_key &&
          (!inventoryDigest || item.source_inventory_digest === inventoryDigest),
      ) ?? null;
    if (!unit.value) return;
    try {
      decision.value = await getTaskUnitDecision(unit.value.id);
      approvedCandidateId.value = decision.value.approved_candidate_id ?? '';
      rejectedCandidateIds.value = [...decision.value.rejected_candidate_ids];
      note.value = decision.value.note ?? '';
      for (const mapping of decision.value.manual_mappings) {
        manualSources[mapping.torrent_path] = mapping.source_relative_path;
      }
      try {
        verification.value = await getTaskUnitReviewVerification(unit.value.id);
      } catch (error) {
        if (!(error instanceof ApiProblem && error.code === 'REVIEW_VERIFICATION_NOT_FOUND')) {
          throw error;
        }
      }
      try {
        executionGate.value = await getTaskUnitExecutionGate(unit.value.id);
      } catch (error) {
        if (!(error instanceof ApiProblem && error.code === 'EXECUTION_GATE_NOT_FOUND'))
          throw error;
      }
      try {
        executionPlan.value = await getTaskUnitExecutionPlan(unit.value.id);
        targetRoot.value = executionPlan.value.target_root;
        targetDownloaderId.value = executionPlan.value.target_downloader_id ?? '';
      } catch (error) {
        if (!(error instanceof ApiProblem && error.code === 'EXECUTION_PLAN_NOT_FOUND'))
          throw error;
      }
    } catch (error) {
      if (!(error instanceof ApiProblem && error.code === 'REVIEW_NOT_FOUND')) throw error;
    }
  } catch (error) {
    showError(error);
  } finally {
    loading.value = false;
  }
}

async function save(): Promise<void> {
  if (!unit.value || !canSubmit.value) return;
  saving.value = true;
  try {
    const result = await submitTaskUnitDecision(unit.value.id, {
      expected_version: decision.value?.version ?? 0,
      approved_candidate_id: approvedCandidateId.value || null,
      rejected_candidate_ids: rejectedCandidateIds.value.filter(
        (candidateId) => candidateId !== approvedCandidateId.value,
      ),
      manual_mappings: ambiguousTorrentPaths.value
        .map((torrentPath) => ({
          torrent_path: torrentPath,
          source_relative_path: manualSources[torrentPath]?.trim() ?? '',
        }))
        .filter((mapping) => mapping.source_relative_path.length > 0),
      note: note.value.trim() || null,
    });
    decision.value = result;
    verification.value = null;
    executionGate.value = null;
    executionPlan.value = null;
    ElMessage.success(`审核 revision v${result.version} 已保存；未执行任何下载器写操作`);
    emit('saved');
  } catch (error) {
    showError(error);
  } finally {
    saving.value = false;
  }
}

async function reverify(): Promise<void> {
  if (!unit.value || !canReverify.value) return;
  reverifying.value = true;
  try {
    verification.value = await reverifyTaskUnitDecision(unit.value.id);
    executionGate.value = null;
    executionPlan.value = null;
    ElMessage.success(
      `重验证完成：${verification.value.verification_level}；结果仅作为不可变证据保存`,
    );
    emit('saved');
  } catch (error) {
    showError(error);
  } finally {
    reverifying.value = false;
  }
}

async function checkExecutionGate(): Promise<void> {
  if (!unit.value || !decision.value || !props.item.preflight.current) return;
  checkingGate.value = true;
  try {
    executionGate.value = await refreshTaskUnitExecutionGate(unit.value.id);
    executionPlan.value = null;
    ElMessage.success(
      executionGate.value.eligible
        ? 'Pre-execution gate 已通过；本页仍不会启动任何副作用'
        : `Pre-execution gate 已阻断：${executionGate.value.blocked_reasons.join(', ')}`,
    );
  } catch (error) {
    showError(error);
  } finally {
    checkingGate.value = false;
  }
}

async function createExecutionPlan(): Promise<void> {
  if (!unit.value || !canPlan.value) return;
  planning.value = true;
  try {
    executionPlan.value = await createTaskUnitExecutionPlan(
      unit.value.id,
      targetRoot.value.trim(),
      targetDownloaderId.value,
    );
    clearExecuteReplayState();
    ElMessage.success(
      executionPlan.value.ready
        ? '无副作用执行计划已生成；尚未启动任何文件或下载器操作'
        : `执行计划已生成但被阻断：${executionPlan.value.blocked_reasons.join(', ')}`,
    );
  } catch (error) {
    showError(error);
  } finally {
    planning.value = false;
  }
}

async function executeExecutionPlan(): Promise<void> {
  if (!unit.value || executing.value) return;
  const replaying = canReplayExecute.value;
  executing.value = true;
  try {
    if (replaying) {
      const result = await executeTask(
        props.item.task.id,
        executeReplayPlanId.value,
        executeIdempotencyKey.value,
      );
      executeResultUnknown.value = false;
      lastMutation.value = result;
      ElMessage.success(
        `执行结果已确认：${result.status}${result.idempotency_replayed ? '（幂等重放）' : ''}`,
      );
      emit('saved');
      return;
    }
    if (!executionPlan.value) return;
    const latest = await getTaskUnitExecutionPlan(unit.value.id);
    executionPlan.value = latest;
    targetRoot.value = latest.target_root;
    targetDownloaderId.value = latest.target_downloader_id ?? '';
    if (!latest.ready || !latest.current || props.item.task.status !== 'AWAITING_CONFIRMATION') {
      ElMessage.warning('当前 execution plan 已不满足 READY + CURRENT + AWAITING_CONFIRMATION');
      return;
    }
    const downloader = targetDownloaders.value.find(
      (item) => item.id === latest.target_downloader_id,
    );
    if (!downloader) {
      ElMessage.warning('计划绑定的目标 qBittorrent 当前不再满足启用/连接/路径安全门');
      return;
    }
    const verificationNote = latest.client_check_required
      ? '添加后必须执行完整 qBittorrent 客户端校验，不允许跳过。'
      : '仅 FULL_VERIFIED 且目标客户端能力仍允许时才可能跳过客户端校验。';
    try {
      await ElMessageBox.confirm(
        `将执行不可变计划 ${latest.plan_digest.slice(0, 20)}…。目标：${downloader.name} v${downloader.version}；${latest.hardlink_count} 个 hardlink、${latest.create_directory_count} 个目录、${latest.client_fetch_count} 个客户端补齐，预计客户端下载上界 ${formatByteUpperBound(latest.estimated_download_bytes_upper_bound)}。${verificationNote} 源文件保持只读；真实副作用由 operation journal 驱动并可恢复。`,
        '确认执行当前计划',
        {
          confirmButtonText: '执行当前计划',
          cancelButtonText: '返回检查',
          type: 'warning',
        },
      );
    } catch {
      return;
    }
    if (!executeIdempotencyKey.value) {
      executeIdempotencyKey.value = createTaskActionIdempotencyKey('execute', props.item.task.id);
    }
    executeReplayPlanId.value = latest.id;
    const result = await executeTask(props.item.task.id, latest.id, executeIdempotencyKey.value);
    executeResultUnknown.value = false;
    lastMutation.value = result;
    ElMessage.success(
      `执行动作已受理：${result.status}${result.idempotency_replayed ? '（幂等重放）' : ''}`,
    );
    emit('saved');
  } catch (error) {
    if (
      isUnknownMutationResult(error) &&
      executeIdempotencyKey.value &&
      executeReplayPlanId.value
    ) {
      executeResultUnknown.value = true;
      ElMessage.warning(
        '执行响应结果未知；只能使用已冻结的 plan 与同一 Idempotency-Key 重试确认结果',
      );
    }
    showError(error);
  } finally {
    executing.value = false;
  }
}

async function cancelAndRollback(): Promise<void> {
  if (!canCancel.value) return;
  cancelling.value = true;
  try {
    const downloaderChoice = removeDownloaderTask.value
      ? '移除当前任务对应的 PackBreaker qBittorrent 任务，固定 deleteFiles=false，不删除磁盘数据'
      : '保留下载器任务';
    const resourceChoice = rollbackCreatedResources.value
      ? '仅回滚 operation journal 明确拥有的 hardlink 与空目录；证据变化时失败关闭'
      : '保留 PackBreaker 已创建的 hardlink/目录';
    try {
      await ElMessageBox.confirm(
        `${downloaderChoice}；${resourceChoice}。源媒体不在删除范围内，也不会被打开写入。`,
        '确认取消与回滚范围',
        {
          confirmButtonText: '按以上范围取消',
          cancelButtonText: '返回检查',
          type: 'warning',
        },
      );
    } catch {
      return;
    }
    if (!cancelIdempotencyKey.value) {
      cancelIdempotencyKey.value = createTaskActionIdempotencyKey('cancel', props.item.task.id);
    }
    const result = await cancelTask(
      props.item.task.id,
      {
        remove_downloader_task: removeDownloaderTask.value,
        rollback_created_resources: rollbackCreatedResources.value,
      },
      cancelIdempotencyKey.value,
    );
    lastMutation.value = result;
    ElMessage.success(
      `取消动作已完成：${result.status}${result.idempotency_replayed ? '（幂等重放）' : ''}`,
    );
    emit('saved');
  } catch (error) {
    showError(error);
  } finally {
    cancelling.value = false;
  }
}

function resetForm(): void {
  unit.value = null;
  decision.value = null;
  verification.value = null;
  executionGate.value = null;
  executionPlan.value = null;
  targetDownloaders.value = [];
  targetDownloaderId.value = '';
  approvedCandidateId.value = '';
  rejectedCandidateIds.value = [];
  note.value = '';
  targetRoot.value = '';
  for (const key of Object.keys(manualSources)) delete manualSources[key];
}

function clearExecuteReplayState(): void {
  executeIdempotencyKey.value = '';
  executeReplayPlanId.value = '';
  executeResultUnknown.value = false;
}

function resetActionState(): void {
  clearExecuteReplayState();
  cancelIdempotencyKey.value = '';
  removeDownloaderTask.value = false;
  rollbackCreatedResources.value = false;
  cancellationAcknowledged.value = false;
  lastMutation.value = null;
}

function isUnknownMutationResult(error: unknown): boolean {
  return (
    error instanceof ApiProblem &&
    (error.code === 'API_UNAVAILABLE' || error.status === 408 || (error.status ?? 0) >= 500)
  );
}

function ambiguousPaths(candidate: TaskCandidate | null): string[] {
  if (!candidate) return [];
  const mappings = candidate.evidence['mappings'];
  if (!Array.isArray(mappings)) return [];
  return mappings.flatMap((mapping) => {
    if (
      typeof mapping === 'object' &&
      mapping !== null &&
      'state' in mapping &&
      mapping.state === 'AMBIGUOUS' &&
      'torrent_path' in mapping &&
      typeof mapping.torrent_path === 'string'
    ) {
      return [mapping.torrent_path];
    }
    return [];
  });
}

function preflightInventoryDigest(item: PreflightReviewItem): string | null {
  const value = item.preflight.payload['source_inventory_digest'];
  return typeof value === 'string' ? value : null;
}

function showError(error: unknown): void {
  if (error instanceof ApiProblem) {
    ElMessage.error(`${error.code}：${error.message}`);
    return;
  }
  ElMessage.error(error instanceof Error ? error.message : '审核请求失败');
}
</script>

<template>
  <section class="review-editor" v-loading="loading">
    <div class="review-editor-heading">
      <div>
        <h3>人工审核 revision</h3>
        <small v-if="decision">当前 v{{ decision.version }} · {{ decision.actor_kind }}</small>
        <small v-else>尚无人工审核 revision</small>
      </div>
      <el-tag type="info">审核 revision 本身不触发执行</el-tag>
    </div>

    <el-alert
      v-if="!item.preflight.current"
      title="Preflight 已失效，禁止提交审核"
      type="error"
      :closable="false"
      show-icon
    />
    <el-alert
      v-else-if="!stateAllowsReview"
      :title="`任务当前为 ${item.task.status}，不允许修改审核 revision`"
      description="审核 API 只允许 PREFLIGHT 打开 REVIEW_OPENED bridge，或在 AWAITING_CONFIRMATION 中追加 revision；已进入副作用链的任务只能观察证据或按 journal 安全取消。"
      type="warning"
      :closable="false"
      show-icon
    />
    <el-alert
      v-else-if="!unit"
      title="当前 Preflight 对应的 TaskUnit 未找到"
      type="error"
      :closable="false"
      show-icon
    />

    <el-form label-position="top">
      <el-form-item label="批准候选（最多一个）">
        <el-radio-group v-model="approvedCandidateId">
          <el-radio value="">暂不批准</el-radio>
          <el-radio
            v-for="candidate in eligibleCandidates"
            :key="candidate.id"
            :value="candidate.id"
          >
            {{ candidate.site_id }} · {{ candidate.display_name }} · {{ candidate.score }} 分
          </el-radio>
        </el-radio-group>
      </el-form-item>

      <el-form-item label="显式拒绝候选">
        <el-checkbox-group v-model="rejectedCandidateIds">
          <el-checkbox
            v-for="candidate in item.candidates"
            :key="candidate.id"
            :value="candidate.id"
            :disabled="candidate.id === approvedCandidateId"
          >
            {{ candidate.site_id }} · {{ candidate.display_name }}
            <span v-if="candidate.rejected">（算法硬拒绝）</span>
          </el-checkbox>
        </el-checkbox-group>
      </el-form-item>

      <template v-if="ambiguousTorrentPaths.length">
        <el-alert
          title="人工映射会要求重新验证"
          description="只允许填写 source_root 内的相对路径；服务端还会确认该文件属于对应 AMBIGUOUS 候选集合。"
          type="warning"
          :closable="false"
          show-icon
        />
        <el-form-item
          v-for="torrentPath in ambiguousTorrentPaths"
          :key="torrentPath"
          :label="`映射 ${torrentPath}`"
        >
          <el-input
            v-model="manualSources[torrentPath]"
            placeholder="例如 one/Movie.2026.mkv（相对于 Analyze 的 source_root）"
          />
        </el-form-item>
      </template>

      <el-form-item label="审核备注">
        <el-input v-model="note" type="textarea" :rows="3" maxlength="2000" show-word-limit />
      </el-form-item>
    </el-form>

    <div class="review-editor-actions">
      <div class="review-editor-status">
        <span v-if="decision?.requires_reverification">当前 revision 标记为需要重新验证</span>
        <span v-else-if="decision">当前 revision 不要求重新验证，但仍不授予执行权限</span>
        <span v-if="verification">
          重验证 {{ verification.verification_level }} ·
          {{ verification.verification_digest.slice(0, 20) }}…
        </span>
      </div>
      <div class="review-editor-buttons">
        <el-button
          v-if="decision?.requires_reverification"
          :disabled="!canReverify"
          :loading="reverifying"
          @click="reverify"
        >
          重新验证当前 revision
        </el-button>
        <el-button type="primary" :disabled="!canSubmit" :loading="saving" @click="save">
          保存审核 revision
        </el-button>
      </div>
    </div>

    <div v-if="decision" class="execution-gate-card">
      <div class="review-editor-heading">
        <div>
          <h3>Pre-execution gate</h3>
          <small>只生成进入后续安全准备阶段的资格证据，不创建目录、链接或下载器任务</small>
        </div>
        <el-button
          :disabled="!item.preflight.current || !unit"
          :loading="checkingGate"
          @click="checkExecutionGate"
        >
          刷新执行门检查
        </el-button>
      </div>
      <template v-if="executionGate">
        <div class="gate-tags">
          <el-tag :type="executionGate.eligible ? 'success' : 'danger'">
            {{ executionGate.eligible ? 'ELIGIBLE' : 'BLOCKED' }}
          </el-tag>
          <el-tag :type="executionGate.current ? 'success' : 'warning'">
            {{ executionGate.current ? 'CURRENT' : 'STALE' }}
          </el-tag>
          <el-tag v-if="executionGate.client_check_required" type="warning">
            CLIENT CHECK REQUIRED
          </el-tag>
          <el-tag type="info">side_effects_started = false</el-tag>
        </div>
        <div class="review-editor-status">
          <span v-if="executionGate.verification_level">
            验证等级 {{ executionGate.verification_level }} · 来源
            {{ executionGate.verification_source ?? 'UNKNOWN' }}
          </span>
          <span v-if="executionGate.blocked_reasons.length" class="gate-blocked">
            阻断原因：{{ executionGate.blocked_reasons.join('；') }}
          </span>
          <span v-if="executionGate.preflight_stale_reasons.length" class="gate-blocked">
            Preflight 失效：{{ executionGate.preflight_stale_reasons.join('；') }}
          </span>
          <span>gate {{ executionGate.gate_digest.slice(0, 20) }}…</span>
        </div>
      </template>
      <small v-else>尚未生成执行门证据。</small>
    </div>

    <div v-if="executionGate?.eligible" class="execution-gate-card">
      <div class="review-editor-heading">
        <div>
          <h3>Execution plan preview</h3>
          <small
            >生成计划本身无副作用；只有 READY + CURRENT
            的当前计划才能通过下方显式确认进入真实执行链</small
          >
        </div>
        <el-button :disabled="!canPlan" :loading="planning" @click="createExecutionPlan">
          生成无副作用计划
        </el-button>
      </div>
      <el-form label-position="top">
        <el-form-item label="目标 qBittorrent（必须已通过连接与路径安全门）">
          <el-select v-model="targetDownloaderId" placeholder="选择目标 qBittorrent" filterable>
            <el-option
              v-for="downloader in targetDownloaders"
              :key="downloader.id"
              :label="`${downloader.name} · v${downloader.version}`"
              :value="downloader.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="目标根（相对于 /data，目录必须已存在）">
          <el-input v-model="targetRoot" placeholder="例如 seeding/movies" />
        </el-form-item>
      </el-form>
      <template v-if="executionPlan">
        <div class="gate-tags">
          <el-tag :type="executionPlan.ready ? 'success' : 'danger'">
            {{ executionPlan.ready ? 'READY' : 'BLOCKED' }}
          </el-tag>
          <el-tag :type="executionPlan.current ? 'success' : 'warning'">
            {{ executionPlan.current ? 'CURRENT' : 'STALE' }}
          </el-tag>
          <el-tag v-if="executionPlan.client_check_required" type="warning">
            CLIENT CHECK REQUIRED
          </el-tag>
          <el-tag type="info">计划生成无副作用</el-tag>
        </div>
        <div class="review-editor-status">
          <span>
            {{ executionPlan.hardlink_count }} 个硬链接计划 ·
            {{ executionPlan.client_fetch_count }} 个客户端补齐 ·
            {{ executionPlan.create_directory_count }} 个待创建目录
          </span>
          <span>
            预计客户端下载上界
            {{ formatByteUpperBound(executionPlan.estimated_download_bytes_upper_bound) }}
          </span>
          <span v-if="executionPlan.target_remote_save_path">
            qB 保存路径 {{ executionPlan.target_remote_save_path }}
          </span>
          <span v-if="executionPlan.blocked_reasons.length" class="gate-blocked">
            计划阻断：{{ executionPlan.blocked_reasons.join('；') }}
          </span>
          <span v-if="executionPlan.current_reasons.length" class="gate-blocked">
            当前性变化：{{ executionPlan.current_reasons.join('；') }}
          </span>
          <span>plan {{ executionPlan.plan_digest.slice(0, 20) }}…</span>
        </div>
        <el-table :data="executionPlan.actions" size="small" empty-text="没有文件动作">
          <el-table-column prop="torrent_path" label="Torrent 路径" min-width="220" />
          <el-table-column prop="kind" label="计划动作" width="150" />
          <el-table-column prop="length" label="字节" width="110" />
          <el-table-column prop="source_relative_path" label="源相对路径" min-width="220">
            <template #default="{ row }">{{ row.source_relative_path ?? '—' }}</template>
          </el-table-column>
        </el-table>
        <div class="mutation-action-panel">
          <el-alert
            title="这是实际执行入口"
            description="点击后会再次读取 latest execution plan，并由后端重新核对 plan/gate/review/source/目标下载器绑定。通过确认后才可能创建 journal-owned 目录/硬链接并向 qBittorrent 写入；源文件始终只读。"
            type="warning"
            :closable="false"
            show-icon
          />
          <el-alert
            v-if="executeResultUnknown"
            title="上一次执行请求结果未知"
            description="已冻结原 execution plan ID 与 Idempotency-Key。这里只允许原请求幂等重放以确认结果，不会重新授权、切换计划或创建第二套资源。"
            type="error"
            :closable="false"
            show-icon
          />
          <div class="review-editor-status">
            <span>验证等级 {{ executionPlan.verification_level }}</span>
            <span v-if="selectedTargetDownloader">
              目标 {{ selectedTargetDownloader.name }} · v{{ selectedTargetDownloader.version }}
            </span>
            <span v-if="executionPlan.client_check_required" class="gate-blocked">
              本计划必须完整执行客户端校验，禁止 skip-check。
            </span>
            <span v-if="lastMutation">
              最近动作 {{ lastMutation.action }} → {{ lastMutation.status }} · receipt
              {{ lastMutation.receipt_id.slice(0, 12) }}…
            </span>
          </div>
          <div class="mutation-action-buttons">
            <small v-if="executeResultUnknown">
              当前任务可能已进入后续状态；重试仅查询/收敛第一次请求的持久化 receipt。
            </small>
            <small v-else-if="!canExecute">
              仅 AWAITING_CONFIRMATION 且计划 READY + CURRENT、目标下载器仍通过安全门时可首次执行。
            </small>
            <el-button
              type="warning"
              :disabled="!canExecute"
              :loading="executing"
              @click="executeExecutionPlan"
            >
              {{ executeResultUnknown ? '重试确认执行结果' : '确认并执行当前计划' }}
            </el-button>
          </div>
        </div>
      </template>
      <small v-else>尚未生成执行计划。</small>
    </div>
    <div v-if="showCancellationPanel" class="execution-gate-card cancellation-card">
      <div class="review-editor-heading">
        <div>
          <h3>取消 / 回滚</h3>
          <small
            >取消选项会被后端冻结到 ROLLING_BACK checkpoint；文件回滚只处理 operation journal
            明确拥有的资源</small
          >
        </div>
        <el-tag :type="cancellationInProgress ? 'warning' : 'danger'">
          {{ cancellationInProgress ? item.task.status : 'SIDE EFFECTS ACTIVE' }}
        </el-tag>
      </div>

      <el-alert
        v-if="cancellationInProgress"
        title="取消/回滚已经开始"
        description="当前选项已在服务端冻结。前端不会重新提交另一组 remove/rollback 选项；后台 driver 会按既有 checkpoint 幂等恢复。"
        type="warning"
        :closable="false"
        show-icon
      />
      <template v-else>
        <el-alert
          title="取消不会删除源媒体"
          description="移除 qBittorrent 任务时固定 deleteFiles=false。回滚仅删除当前 task 的 journal-owned hardlink 与空目录；任何所有权/快照不确定都会失败关闭。"
          type="info"
          :closable="false"
          show-icon
        />
        <div class="cancellation-options">
          <label class="cancellation-option">
            <el-checkbox v-model="removeDownloaderTask">
              移除 PackBreaker 创建的 qBittorrent 任务
            </el-checkbox>
            <small>固定 deleteFiles=false；只移除客户端任务记录，不删除磁盘数据。</small>
          </label>
          <label class="cancellation-option">
            <el-checkbox v-model="rollbackCreatedResources">
              回滚 PackBreaker 创建的 hardlink 与空目录
            </el-checkbox>
            <small
              >仅按 operation journal ID 逆序回滚；外部替换、非空目录或证据不确定会阻断。</small
            >
          </label>
        </div>
        <el-alert
          v-if="!cancellationOptionsConsistent"
          title="回滚文件前必须同时移除下载器任务"
          description="目标 qBittorrent 可能仍持有这些路径；为避免客户端与文件系统竞态，前端不允许只回滚链接。"
          type="error"
          :closable="false"
          show-icon
        />
        <el-checkbox v-model="cancellationAcknowledged" :disabled="!cancellationOptionsConsistent">
          我已确认上面的移除/保留范围，并理解未勾选的资源将被保留
        </el-checkbox>
        <div class="mutation-action-buttons">
          <small>任务状态：{{ item.task.status }}。选项变化后必须重新确认影响范围。</small>
          <el-button
            type="danger"
            :disabled="!canCancel"
            :loading="cancelling"
            @click="cancelAndRollback"
          >
            按已确认范围取消任务
          </el-button>
        </div>
      </template>
    </div>
    <TaskEventTimeline
      :task-id="item.task.id"
      :live="props.live"
      @changed="emit('event', $event)"
    />
  </section>
</template>

<style scoped>
.review-editor {
  display: grid;
  gap: 16px;
  margin-bottom: 24px;
  padding-bottom: 24px;
  border-bottom: 1px solid var(--line);
}
.review-editor-heading,
.review-editor-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.review-editor-heading h3 {
  margin: 0 0 4px;
}
.review-editor-heading small,
.review-editor-actions span {
  color: var(--muted);
  font-size: 11px;
}
.review-editor-status {
  display: grid;
  gap: 4px;
}
.review-editor-buttons {
  display: flex;
  gap: 8px;
}
.execution-gate-card {
  display: grid;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface);
}
.mutation-action-panel,
.cancellation-options {
  display: grid;
  gap: 10px;
}
.mutation-action-panel {
  margin-top: 4px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}
.mutation-action-buttons {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.mutation-action-buttons small,
.cancellation-option small {
  color: var(--muted);
  font-size: 11px;
}
.cancellation-option {
  display: grid;
  gap: 3px;
  padding: 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
}
.cancellation-card {
  border-color: color-mix(in srgb, var(--red) 35%, var(--line));
}
.gate-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.gate-blocked {
  color: var(--red);
}
.review-editor :deep(.el-radio-group),
.review-editor :deep(.el-checkbox-group) {
  display: grid;
  gap: 8px;
  align-items: start;
}
.review-editor :deep(.el-radio),
.review-editor :deep(.el-checkbox) {
  height: auto;
  white-space: normal;
}
@media (max-width: 720px) {
  .review-editor-heading,
  .review-editor-actions {
    align-items: stretch;
    flex-direction: column;
  }
  .mutation-action-buttons {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
