<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';

import { ApiProblem } from '../api/client';
import {
  getTaskUnitDecision,
  listTaskUnits,
  submitTaskUnitDecision,
  type TaskCandidate,
  type TaskReview,
  type TaskUnit,
} from '../api/tasks';
import type { PreflightReviewItem } from '../preflightReviews';

const props = defineProps<{ item: PreflightReviewItem }>();
const emit = defineEmits<{ saved: [] }>();

const unit = ref<TaskUnit | null>(null);
const decision = ref<TaskReview | null>(null);
const approvedCandidateId = ref('');
const rejectedCandidateIds = ref<string[]>([]);
const note = ref('');
const manualSources = reactive<Record<string, string>>({});
const loading = ref(false);
const saving = ref(false);

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
    const units = await listTaskUnits(props.item.task.id);
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
    ElMessage.success(`审核 revision v${result.version} 已保存；未执行任何下载器写操作`);
    emit('saved');
  } catch (error) {
    showError(error);
  } finally {
    saving.value = false;
  }
}

function resetForm(): void {
  unit.value = null;
  decision.value = null;
  approvedCandidateId.value = '';
  rejectedCandidateIds.value = [];
  note.value = '';
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
      <span v-if="decision?.requires_reverification">当前 revision 标记为需要重新验证</span>
      <span v-else-if="decision">当前 revision 不要求重新验证，但仍不授予执行权限</span>
      <el-button type="primary" :disabled="!canSubmit" :loading="saving" @click="save">
        保存审核 revision
      </el-button>
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
