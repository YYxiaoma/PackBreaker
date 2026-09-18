<script setup lang="ts">
import { ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { RefreshCw } from '@lucide/vue';

import { ApiProblem } from '../api/client';
import { getTask, getTaskPreflight, listTaskCandidates, type TaskRecord } from '../api/tasks';
import { buildPreflightReview, type PreflightReviewItem } from '../preflightReviews';
import TaskAnalysisPanel from './TaskAnalysisPanel.vue';
import TaskOperationCenter from './TaskOperationCenter.vue';
import TaskReviewEditor from './TaskReviewEditor.vue';

const props = defineProps<{ taskId: string }>();

const review = ref<PreflightReviewItem | null>(null);
const task = ref<TaskRecord | null>(null);
const loading = ref(false);
const errorCode = ref('');
const operationRefreshKey = ref(0);

watch(
  () => props.taskId,
  () => void refresh(),
  { immediate: true },
);

async function refresh(): Promise<void> {
  const taskId = props.taskId.trim();
  if (!taskId) {
    review.value = null;
    task.value = null;
    return;
  }
  loading.value = true;
  errorCode.value = '';
  try {
    const loadedTask = await getTask(taskId);
    task.value = loadedTask;
    try {
      const [preflight, candidates] = await Promise.all([
        getTaskPreflight(taskId),
        listTaskCandidates(taskId),
      ]);
      review.value = buildPreflightReview(loadedTask, preflight, candidates);
    } catch (error) {
      if (error instanceof ApiProblem && error.code === 'PREFLIGHT_NOT_FOUND') {
        review.value = null;
      } else {
        throw error;
      }
    }
  } catch (error) {
    review.value = null;
    task.value = null;
    errorCode.value = error instanceof ApiProblem ? error.code : 'API_REQUEST_FAILED';
    ElMessage.error(error instanceof Error ? error.message : '审核证据读取失败');
  } finally {
    loading.value = false;
  }
}

async function handleEvidenceChanged(): Promise<void> {
  operationRefreshKey.value += 1;
  await refresh();
}
</script>

<template>
  <section class="execution-evidence-panel" v-loading="loading">
    <div class="execution-evidence-heading">
      <div>
        <h3>审核、校验与对账</h3>
        <p>所有证据与安全动作都绑定当前底层 Run，不再依赖独立“预演”或“清理”页面。</p>
      </div>
      <el-button :loading="loading" @click="refresh"><RefreshCw :size="15" />刷新证据</el-button>
    </div>

    <el-alert
      v-if="errorCode"
      :title="`当前 Run 证据暂不可用：${errorCode}`"
      type="warning"
      :closable="false"
      show-icon
    />

    <template v-if="task">
      <el-alert
        v-if="!review"
        title="当前 Run 尚无可审核的 Preflight 快照"
        description="可在下方分析面板执行只读分析；生成快照后刷新本区域即可继续候选确认、Plan 与执行门检查。"
        type="info"
        :closable="false"
        show-icon
      />
      <TaskReviewEditor
        v-if="review"
        :item="review"
        :live="true"
        @saved="handleEvidenceChanged"
        @event="handleEvidenceChanged"
      />
      <TaskAnalysisPanel :suggested-task-id="taskId" />
      <TaskOperationCenter :task-id="taskId" :refresh-key="operationRefreshKey" />
    </template>
  </section>
</template>

<style scoped>
.execution-evidence-panel {
  display: grid;
  gap: 14px;
}
.execution-evidence-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.execution-evidence-heading h3,
.execution-evidence-heading p {
  margin: 0;
}
.execution-evidence-heading p {
  margin-top: 5px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}
@media (max-width: 760px) {
  .execution-evidence-heading {
    flex-direction: column;
  }
}
</style>
