<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';

import { ApiProblem } from '../api/client';
import { listDownloaders, type Downloader } from '../api/downloaders';
import {
  createTaskUnitExecutionPlan,
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
  type TaskReview,
  type TaskUnit,
} from '../api/tasks';
import type { PreflightReviewItem } from '../preflightReviews';

const props = defineProps<{ item: PreflightReviewItem }>();
const emit = defineEmits<{ saved: [] }>();

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
const targetRoot = ref('');

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

watch(
  () => props.item.task.id,
  () => void load(),
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
      <el-tag type="info">execution_allowed = false</el-tag>
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
      :title="`任务当前为 ${item.task.status}，尚未进入 PREFLIGHT 审核状态`"
      description="审核 API 只允许 PREFLIGHT 打开 REVIEW_OPENED bridge，或在 AWAITING_CONFIRMATION 中追加 revision；不会绕过状态机。"
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
          <small>只读检查目标树并生成不可变计划；不会创建目录、硬链接或下载器任务</small>
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
          <el-tag type="info">execution_allowed = false</el-tag>
          <el-tag type="info">side_effects_started = false</el-tag>
        </div>
        <div class="review-editor-status">
          <span>
            {{ executionPlan.hardlink_count }} 个硬链接计划 ·
            {{ executionPlan.client_fetch_count }} 个客户端补齐 ·
            {{ executionPlan.create_directory_count }} 个待创建目录
          </span>
          <span>预计客户端下载上界 {{ executionPlan.estimated_download_bytes_upper_bound }} B</span>
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
      </template>
      <small v-else>尚未生成执行计划。</small>
    </div>
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
}
</style>
