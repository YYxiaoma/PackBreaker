<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { AlertTriangle, CheckCircle2, RefreshCw, Search, ShieldCheck } from '@lucide/vue';

import { ApiProblem } from '../api/client';
import { getTaskPreflight, listTaskCandidates, listTasks, type TaskRecord } from '../api/tasks';
import {
  buildPreflightReview,
  type PreflightReviewItem,
  type PreflightReviewLevel,
} from '../preflightReviews';
import TaskAnalysisPanel from './TaskAnalysisPanel.vue';
import TaskReviewEditor from './TaskReviewEditor.vue';

const reviews = ref<PreflightReviewItem[]>([]);
const loadErrors = ref<string[]>([]);
const loading = ref(false);
const query = ref('');
const level = ref<PreflightReviewLevel | ''>('');
const active = ref<PreflightReviewItem | null>(null);
const drawerVisible = ref(false);

const filtered = computed(() => {
  const needle = query.value.trim().toLowerCase();
  return reviews.value.filter((item) => {
    if (level.value && item.level !== level.value) return false;
    if (!needle) return true;
    return [
      item.task.id,
      item.task.type,
      item.task.source_hash,
      item.task.normalized_unit_key,
      item.primaryCandidate?.display_name ?? '',
      item.primaryCandidate?.site_id ?? '',
    ]
      .join(' ')
      .toLowerCase()
      .includes(needle);
  });
});

const currentCount = computed(() => reviews.value.filter((item) => item.preflight.current).length);
const staleCount = computed(() => reviews.value.filter((item) => !item.preflight.current).length);
const fullCount = computed(
  () => reviews.value.filter((item) => item.level === 'FULL_VERIFIED').length,
);
const attentionCount = computed(
  () =>
    reviews.value.filter((item) =>
      ['STALE', 'BLOCKED', 'CLIENT_CHECK_REQUIRED', 'NOT_VERIFIED'].includes(item.level),
    ).length,
);

onMounted(() => void refresh());

async function refresh(): Promise<void> {
  loading.value = true;
  loadErrors.value = [];
  try {
    const tasks = await listTasks();
    const collected: PreflightReviewItem[] = [];
    for (let offset = 0; offset < tasks.length; offset += 4) {
      const batch = await Promise.all(tasks.slice(offset, offset + 4).map(loadTaskReview));
      for (const item of batch) {
        if (item) collected.push(item);
      }
    }
    reviews.value = collected.sort(
      (left, right) =>
        reviewPriority(left.level) - reviewPriority(right.level) ||
        right.preflight.created_at.localeCompare(left.preflight.created_at) ||
        left.task.id.localeCompare(right.task.id),
    );
  } catch (error) {
    showError(error);
  } finally {
    loading.value = false;
  }
}

async function loadTaskReview(task: TaskRecord): Promise<PreflightReviewItem | null> {
  try {
    const preflight = await getTaskPreflight(task.id);
    const candidates = await listTaskCandidates(task.id);
    return buildPreflightReview(task, preflight, candidates);
  } catch (error) {
    if (error instanceof ApiProblem && error.code === 'PREFLIGHT_NOT_FOUND') return null;
    const code = error instanceof ApiProblem ? error.code : 'API_REQUEST_FAILED';
    loadErrors.value.push(`${task.id}: ${code}`);
    return null;
  }
}

function open(item: PreflightReviewItem): void {
  active.value = item;
  drawerVisible.value = true;
}

function showError(error: unknown): void {
  if (error instanceof ApiProblem) {
    ElMessage.error(`${error.code}：${error.message}`);
    return;
  }
  ElMessage.error(error instanceof Error ? error.message : '预演聚合请求失败');
}

function reviewPriority(value: PreflightReviewLevel): number {
  return {
    STALE: 0,
    BLOCKED: 1,
    CLIENT_CHECK_REQUIRED: 2,
    NOT_VERIFIED: 3,
    FULL_VERIFIED: 4,
  }[value];
}

function levelType(value: PreflightReviewLevel): 'success' | 'warning' | 'danger' | 'info' {
  if (value === 'FULL_VERIFIED') return 'success';
  if (value === 'STALE' || value === 'BLOCKED') return 'danger';
  if (value === 'CLIENT_CHECK_REQUIRED') return 'warning';
  return 'info';
}

function staleReason(reason: string): string {
  return (
    {
      TASK_VERSION_CHANGED: '任务版本变化',
      SOURCE_CHANGED: '源文件变化',
      SOURCE_UNAVAILABLE: '源目录不可用',
      SITE_CONFIG_CHANGED: '站点配置变化',
      UNIT_RECORD_MISSING: '处理单元证据缺失',
      PREFLIGHT_EVIDENCE_INVALID: '预演证据异常',
    }[reason] ?? reason
  );
}
</script>

<template>
  <section class="task-section review-center">
    <div class="section-heading">
      <h2>真实预演证据 <small>只读聚合 SQLite 任务、Preflight 与 Candidate</small></h2>
      <el-button :loading="loading" @click="refresh"><RefreshCw :size="16" />刷新</el-button>
    </div>

    <el-alert
      title="当前没有启动副作用的执行动作"
      description="Analyze 已按状态机推进到 PREFLIGHT；提交首个审核 revision 后进入 AWAITING_CONFIRMATION。本页可以生成只读 pre-execution gate 资格证据，但不会进入 LINKING、创建硬链接或调用下载器写接口。"
      type="info"
      :closable="false"
      show-icon
    />

    <div class="review-stats">
      <div>
        <small>已有快照</small><b>{{ reviews.length }}</b>
      </div>
      <div>
        <small>当前有效</small><b>{{ currentCount }}</b>
      </div>
      <div>
        <small>已失效</small><b>{{ staleCount }}</b>
      </div>
      <div>
        <small>FULL_VERIFIED</small><b>{{ fullCount }}</b>
      </div>
      <div>
        <small>需关注</small><b>{{ attentionCount }}</b>
      </div>
    </div>

    <el-alert
      v-if="loadErrors.length"
      :title="`${loadErrors.length} 个任务的审核证据加载失败`"
      :description="loadErrors.join('；')"
      type="warning"
      :closable="false"
      show-icon
    />

    <div class="review-filters">
      <el-input v-model="query" clearable placeholder="搜索 task、source hash、候选标题或站点…">
        <template #prefix><Search :size="16" /></template>
      </el-input>
      <el-select v-model="level" clearable placeholder="全部审核等级">
        <el-option
          v-for="item in [
            'STALE',
            'BLOCKED',
            'CLIENT_CHECK_REQUIRED',
            'NOT_VERIFIED',
            'FULL_VERIFIED',
          ]"
          :key="item"
          :label="item"
          :value="item"
        />
      </el-select>
    </div>

    <el-table
      v-loading="loading"
      :data="filtered"
      row-key="task.id"
      empty-text="暂无真实 preflight"
    >
      <el-table-column label="任务" min-width="275">
        <template #default="{ row }">
          <div class="review-identity">
            <button @click="open(row)">{{ row.task.type }}</button>
            <code>{{ row.task.id }}</code>
            <small>{{ row.task.status }} · v{{ row.task.version }}</small>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="Preflight" min-width="240">
        <template #default="{ row }">
          <div class="review-preflight">
            <el-tag :type="row.preflight.current ? 'success' : 'danger'">
              {{ row.preflight.current ? 'CURRENT' : 'STALE' }}
            </el-tag>
            <code>{{ row.preflight.snapshot_digest.slice(0, 20) }}…</code>
            <small>{{ new Date(row.preflight.created_at).toLocaleString() }}</small>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="审核等级" width="205">
        <template #default="{ row }">
          <el-tag :type="levelType(row.level)">{{ row.level }}</el-tag>
          <small v-if="row.hardRejectedCount">硬拒绝 {{ row.hardRejectedCount }} 个候选</small>
        </template>
      </el-table-column>
      <el-table-column label="首选候选" min-width="280">
        <template #default="{ row }">
          <div v-if="row.primaryCandidate" class="review-candidate">
            <b>{{ row.primaryCandidate.display_name }}</b>
            <small>{{ row.primaryCandidate.site_id }} · {{ row.primaryCandidate.score }} 分</small>
          </div>
          <span v-else class="red"><AlertTriangle :size="14" />无可用候选</span>
        </template>
      </el-table-column>
      <el-table-column label="风险 / 当前性" min-width="250">
        <template #default="{ row }">
          <div v-if="!row.preflight.current" class="review-risk red">
            <AlertTriangle :size="14" />
            <span>{{ row.preflight.stale_reasons.map(staleReason).join('；') }}</span>
          </div>
          <div v-else-if="row.level === 'FULL_VERIFIED'" class="review-risk green">
            <CheckCircle2 :size="14" /><span>当前证据已达到完整内容验证</span>
          </div>
          <div v-else class="review-risk">
            <ShieldCheck :size="14" /><span>仍需检查候选验证/错误证据</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="105">
        <template #default="{ row }">
          <el-button link type="primary" @click="open(row)">审核 / 证据</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-drawer
      v-model="drawerVisible"
      size="min(980px, 96vw)"
      :title="active ? `审核证据 ${active.task.id}` : '审核证据'"
    >
      <template v-if="active">
        <TaskReviewEditor :item="active" @saved="refresh" />
        <TaskAnalysisPanel :suggested-task-id="active.task.id" />
      </template>
    </el-drawer>
  </section>
</template>

<style scoped>
.review-center {
  margin-top: 0;
}
.review-stats {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  margin: 18px 0;
}
.review-stats > div {
  display: grid;
  gap: 7px;
  padding: 15px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface);
}
.review-stats small,
.review-identity small,
.review-preflight small,
.review-candidate small,
.review-center td small {
  color: var(--muted);
  font-size: 10px;
}
.review-stats b {
  font-size: 22px;
}
.review-filters {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) 240px;
  gap: 12px;
  margin: 18px 0;
}
.review-identity,
.review-preflight,
.review-candidate {
  display: grid;
  gap: 6px;
}
.review-identity button {
  width: fit-content;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--blue);
  font-weight: 700;
  cursor: pointer;
}
.review-identity code,
.review-preflight code {
  color: var(--muted);
  font-size: 10px;
  overflow-wrap: anywhere;
}
.review-candidate b {
  font-size: 11px;
  line-height: 1.5;
}
.review-risk,
.review-center .red,
.review-center .green {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 11px;
  line-height: 1.5;
}
.review-risk svg {
  flex: 0 0 auto;
  margin-top: 2px;
}
@media (max-width: 900px) {
  .review-stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .review-filters {
    grid-template-columns: 1fr;
  }
}
</style>
