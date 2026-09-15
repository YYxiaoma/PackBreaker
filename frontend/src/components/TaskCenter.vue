<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Plus, RefreshCw, Search } from '@lucide/vue';

import { ApiProblem } from '../api/client';
import {
  cancelTask,
  createTask,
  listTasks,
  releaseTask,
  rerunTask,
  type TaskCreateInput,
  type TaskRecord,
  type TaskStatus,
} from '../api/tasks';
import {
  canCancelBeforeSideEffects,
  cancellationIsCooperativeAnalysis,
  createTaskActionIdempotencyKey,
} from '../taskActionSafety';
import TaskAnalysisPanel from './TaskAnalysisPanel.vue';
import TaskEventTimeline from './TaskEventTimeline.vue';
import TaskOperationCenter from './TaskOperationCenter.vue';

const tasks = ref<TaskRecord[]>([]);
const loading = ref(false);
const createVisible = ref(false);
const detailVisible = ref(false);
const active = ref<TaskRecord | null>(null);
const operationRefreshKey = ref(0);
const preCancelling = ref(false);
const preCancelTaskId = ref('');
const preCancelIdempotencyKey = ref('');
const preCancelResultUnknown = ref(false);
const rerunning = ref(false);
const rerunTaskId = ref('');
const rerunIdempotencyKey = ref('');
const rerunResultUnknown = ref(false);
const releasing = ref(false);
const releaseTaskId = ref('');
const releaseIdempotencyKey = ref('');
const releaseResultUnknown = ref(false);
const query = ref('');
const status = ref<TaskStatus | ''>('');
const creating = ref(false);
const form = reactive<TaskCreateInput>({
  task_type: 'PACKAGE_UNPACK',
  source_downloader_id: '',
  source_hash: '',
  normalized_unit_key: '',
});

const filtered = computed(() => {
  const needle = query.value.trim().toLowerCase();
  return tasks.value.filter((task) => {
    if (status.value && task.status !== status.value) return false;
    if (!needle) return true;
    return [
      task.id,
      task.source_hash,
      task.normalized_unit_key,
      task.source_downloader_id,
      task.type,
    ]
      .join(' ')
      .toLowerCase()
      .includes(needle);
  });
});
const canPreCancelActive = computed(
  () =>
    active.value !== null &&
    (canCancelBeforeSideEffects(active.value.status) ||
      (preCancelResultUnknown.value &&
        preCancelTaskId.value === active.value.id &&
        preCancelIdempotencyKey.value.length > 0)),
);
const activeCancellationIsCooperative = computed(
  () => active.value !== null && cancellationIsCooperativeAnalysis(active.value.status),
);
const canRerunActive = computed(() => {
  const task = active.value;
  if (!task) return false;
  if (rerunResultUnknown.value && rerunTaskId.value === task.id && rerunIdempotencyKey.value) {
    return true;
  }
  if (!['CANCELLED', 'FAILED', 'DONE'].includes(task.status)) return false;
  return !tasks.value.some(
    (item) =>
      item.type === task.type &&
      item.source_downloader_id === task.source_downloader_id &&
      item.source_hash === task.source_hash &&
      item.normalized_unit_key === task.normalized_unit_key &&
      item.run_number > task.run_number,
  );
});
const canReleaseActive = computed(() => {
  const task = active.value;
  if (!task) return false;
  if (
    releaseResultUnknown.value &&
    releaseTaskId.value === task.id &&
    releaseIdempotencyKey.value
  ) {
    return true;
  }
  return task.status === 'DONE';
});

onMounted(() => void refresh());

async function refresh(): Promise<void> {
  const activeId = active.value?.id ?? null;
  loading.value = true;
  try {
    tasks.value = await listTasks();
    if (activeId) {
      active.value = tasks.value.find((task) => task.id === activeId) ?? null;
      if (!active.value) detailVisible.value = false;
    }
  } catch (error) {
    showError(error);
  } finally {
    loading.value = false;
  }
}

async function submitCreate(): Promise<void> {
  if (
    !form.task_type.trim() ||
    !form.source_downloader_id.trim() ||
    !form.source_hash.trim() ||
    !form.normalized_unit_key.trim()
  ) {
    ElMessage.warning('任务类型、来源下载器 ID、source hash 与 unit key 均不能为空');
    return;
  }
  creating.value = true;
  try {
    const result = await createTask({
      task_type: form.task_type.trim(),
      source_downloader_id: form.source_downloader_id.trim(),
      source_hash: form.source_hash.trim(),
      normalized_unit_key: form.normalized_unit_key.trim(),
    });
    await refresh();
    createVisible.value = false;
    open(result.item);
    ElMessage.success(result.created ? '任务已登记' : '相同任务已存在，已打开原任务');
  } catch (error) {
    showError(error);
  } finally {
    creating.value = false;
  }
}

function open(task: TaskRecord): void {
  if (active.value?.id !== task.id) clearPreCancelState();
  if (active.value?.id !== task.id) clearRerunState();
  if (active.value?.id !== task.id) clearReleaseState();
  active.value = task;
  detailVisible.value = true;
}

async function handleTaskEventChanged(): Promise<void> {
  operationRefreshKey.value += 1;
  await refresh();
}

async function cancelBeforeSideEffects(): Promise<void> {
  const task = active.value;
  if (!task || !canPreCancelActive.value || preCancelling.value) return;
  const replaying =
    preCancelResultUnknown.value &&
    preCancelTaskId.value === task.id &&
    preCancelIdempotencyKey.value.length > 0;
  const cooperativeAnalysis = cancellationIsCooperativeAnalysis(task.status);
  preCancelling.value = true;
  try {
    if (!replaying) {
      try {
        await ElMessageBox.confirm(
          cooperativeAnalysis
            ? '将停止当前分析，已有媒体文件不会被修改。'
            : '将取消当前任务，已有媒体文件不会被修改。',
          cooperativeAnalysis ? '确认停止只读分析' : '确认取消未执行任务',
          {
            confirmButtonText: cooperativeAnalysis ? '请求停止只读分析' : '取消未执行任务',
            cancelButtonText: '返回',
            type: 'warning',
          },
        );
      } catch {
        return;
      }
      preCancelTaskId.value = task.id;
      preCancelIdempotencyKey.value = createTaskActionIdempotencyKey('cancel', task.id);
    }
    const result = await cancelTask(
      task.id,
      { remove_downloader_task: false, rollback_created_resources: false },
      preCancelIdempotencyKey.value,
    );
    clearPreCancelState();
    await refresh();
    ElMessage.success(
      `${result.status === 'CANCELLING' ? '取消请求已登记' : '取消结果已确认'}：${result.status}${result.idempotency_replayed ? '（幂等重放）' : ''}`,
    );
  } catch (error) {
    if (isUnknownMutationResult(error) && preCancelIdempotencyKey.value) {
      preCancelResultUnknown.value = true;
      ElMessage.warning('取消结果未知，请使用重试确认原请求结果');
    } else {
      clearPreCancelState();
    }
    showError(error);
  } finally {
    preCancelling.value = false;
  }
}

function clearPreCancelState(): void {
  preCancelTaskId.value = '';
  preCancelIdempotencyKey.value = '';
  preCancelResultUnknown.value = false;
}

function clearRerunState(): void {
  rerunTaskId.value = '';
  rerunIdempotencyKey.value = '';
  rerunResultUnknown.value = false;
}

function clearReleaseState(): void {
  releaseTaskId.value = '';
  releaseIdempotencyKey.value = '';
  releaseResultUnknown.value = false;
}

async function rerunActiveTask(): Promise<void> {
  const task = active.value;
  if (!task || !canRerunActive.value || rerunning.value) return;
  const replaying =
    rerunResultUnknown.value &&
    rerunTaskId.value === task.id &&
    rerunIdempotencyKey.value.length > 0;
  rerunning.value = true;
  try {
    if (!replaying) {
      try {
        await ElMessageBox.confirm(
          `当前 Run #${task.run_number} 将保持 ${task.status} 状态，并创建新的 Run #${task.run_number + 1} 重新处理。`,
          '确认重新运行',
          {
            confirmButtonText: `创建 Run #${task.run_number + 1}`,
            cancelButtonText: '返回',
            type: 'warning',
          },
        );
      } catch {
        return;
      }
      rerunTaskId.value = task.id;
      rerunIdempotencyKey.value = createTaskActionIdempotencyKey('rerun', task.id);
    }
    const result = await rerunTask(task.id, rerunIdempotencyKey.value);
    const childId = result.task_id;
    clearRerunState();
    await refresh();
    const child = tasks.value.find((item) => item.id === childId);
    if (child) open(child);
    ElMessage.success(
      `新的任务 run 已创建：${child?.run_number ? `Run #${child.run_number}` : childId}${result.idempotency_replayed ? '（幂等重放）' : ''}`,
    );
  } catch (error) {
    if (isUnknownMutationResult(error) && rerunIdempotencyKey.value) {
      rerunResultUnknown.value = true;
      ElMessage.warning('重新运行结果未知，请使用重试确认原请求结果');
    } else {
      clearRerunState();
    }
    showError(error);
  } finally {
    rerunning.value = false;
  }
}

async function releaseActiveTask(): Promise<void> {
  const task = active.value;
  if (!task || !canReleaseActive.value || releasing.value) return;
  const replaying =
    releaseResultUnknown.value &&
    releaseTaskId.value === task.id &&
    releaseIdempotencyKey.value.length > 0;
  releasing.value = true;
  try {
    if (!replaying) {
      try {
        await ElMessageBox.confirm(
          '将停止该任务的辅种并清理 PackBreaker 创建的链接与空目录；源媒体不会删除。',
          '确认释放辅种资源',
          {
            confirmButtonText: '释放资源',
            cancelButtonText: '返回',
            type: 'warning',
          },
        );
      } catch {
        return;
      }
      releaseTaskId.value = task.id;
      releaseIdempotencyKey.value = createTaskActionIdempotencyKey('release', task.id);
    }
    const result = await releaseTask(task.id, releaseIdempotencyKey.value);
    clearReleaseState();
    operationRefreshKey.value += 1;
    await refresh();
    ElMessage.success(
      `辅种资源释放已确认：任务仍为 ${result.status}${result.idempotency_replayed ? '（幂等重放）' : ''}`,
    );
  } catch (error) {
    if (isUnknownMutationResult(error) && releaseIdempotencyKey.value) {
      releaseResultUnknown.value = true;
      ElMessage.warning('资源释放结果未知，请使用重试确认原请求结果');
    } else {
      clearReleaseState();
    }
    showError(error);
  } finally {
    releasing.value = false;
  }
}

function isUnknownMutationResult(error: unknown): boolean {
  return (
    error instanceof ApiProblem &&
    (error.code === 'API_UNAVAILABLE' || error.status === 408 || (error.status ?? 0) >= 500)
  );
}

function showError(error: unknown): void {
  if (error instanceof ApiProblem) {
    ElMessage.error(`${error.code}：${error.message}`);
    return;
  }
  ElMessage.error(error instanceof Error ? error.message : '任务请求失败');
}

function statusType(value: TaskStatus): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  if (['DONE', 'SEEDING'].includes(value)) return 'success';
  if (['FAILED', 'CANCELLED'].includes(value)) return 'danger';
  if (['AWAITING_CONFIRMATION', 'PAUSED', 'RETRY'].includes(value)) return 'warning';
  if (value === 'PENDING') return 'info';
  return 'primary';
}

const statusOptions: TaskStatus[] = [
  'PENDING',
  'ANALYZING',
  'SEARCHING',
  'MATCHING',
  'VERIFYING',
  'PREFLIGHT',
  'AWAITING_CONFIRMATION',
  'LINKING',
  'ADDING',
  'CLIENT_VERIFYING',
  'CANCELLING',
  'ROLLING_BACK',
  'PAUSED',
  'RETRY',
  'FAILED',
  'SEEDING',
  'DONE',
  'CANCELLED',
];
</script>

<template>
  <section class="task-section real-task-center">
    <div class="section-heading">
      <h2>任务中心</h2>
      <div>
        <el-button :loading="loading" @click="refresh"><RefreshCw :size="16" />刷新</el-button>
        <el-button type="primary" @click="createVisible = true"
          ><Plus :size="16" />登记任务</el-button
        >
      </div>
    </div>

    <div class="filters real-task-filters">
      <el-input v-model="query" clearable placeholder="搜索 UUID、source hash、unit key…">
        <template #prefix><Search :size="16" /></template>
      </el-input>
      <el-select v-model="status" clearable placeholder="全部状态">
        <el-option v-for="item in statusOptions" :key="item" :label="item" :value="item" />
      </el-select>
    </div>

    <el-table v-loading="loading" :data="filtered" row-key="id" empty-text="暂无任务">
      <el-table-column label="任务" min-width="290">
        <template #default="{ row }">
          <div class="real-task-identity">
            <button @click="open(row)">{{ row.type }}</button>
            <small>Run #{{ row.run_number }}</small>
            <code>{{ row.id }}</code>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="来源" min-width="230">
        <template #default="{ row }">
          <div class="real-task-source">
            <b>{{ row.source_downloader_id }}</b>
            <code>{{ row.source_hash }}</code>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="处理单元" min-width="220">
        <template #default="{ row }"
          ><code>{{ row.normalized_unit_key }}</code></template
        >
      </el-table-column>
      <el-table-column label="状态" width="155">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)">{{ row.status }}</el-tag>
          <small v-if="row.error_code" class="red">{{ row.error_code }}</small>
        </template>
      </el-table-column>
      <el-table-column label="版本 / 更新" width="190">
        <template #default="{ row }">
          <b>v{{ row.version }}</b>
          <small>{{ new Date(row.updated_at).toLocaleString() }}</small>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="100">
        <template #default="{ row }"
          ><el-button link type="primary" @click="open(row)">分析</el-button></template
        >
      </el-table-column>
    </el-table>

    <el-dialog v-model="createVisible" title="登记任务" width="620px">
      <el-form label-position="top">
        <el-form-item label="任务类型">
          <el-input v-model="form.task_type" />
        </el-form-item>
        <el-form-item label="来源下载器 ID">
          <el-input v-model="form.source_downloader_id" placeholder="已配置 downloader 的 UUID" />
        </el-form-item>
        <el-form-item label="Source hash">
          <el-input v-model="form.source_hash" placeholder="来源任务稳定 hash / identity" />
        </el-form-item>
        <el-form-item label="Normalized unit key">
          <el-input
            v-model="form.normalized_unit_key"
            placeholder="由 task unit 识别得到的稳定 key"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">登记任务</el-button>
      </template>
    </el-dialog>

    <el-drawer
      v-model="detailVisible"
      size="min(980px, 96vw)"
      :title="active ? `任务 Run #${active.run_number} · ${active.id}` : '任务'"
    >
      <template v-if="active">
        <el-descriptions :column="2" border class="real-task-summary">
          <el-descriptions-item label="状态">{{ active.status }}</el-descriptions-item>
          <el-descriptions-item label="版本">v{{ active.version }}</el-descriptions-item>
          <el-descriptions-item label="Run">#{{ active.run_number }}</el-descriptions-item>
          <el-descriptions-item label="父任务">{{
            active.parent_task_id ?? '初始 Run'
          }}</el-descriptions-item>
          <el-descriptions-item label="来源下载器">{{
            active.source_downloader_id
          }}</el-descriptions-item>
          <el-descriptions-item label="Source hash">{{ active.source_hash }}</el-descriptions-item>
          <el-descriptions-item label="Unit key" :span="2">{{
            active.normalized_unit_key
          }}</el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-if="canReleaseActive"
          :title="releaseResultUnknown ? '资源释放结果未知' : '可释放辅种资源'"
          :description="
            releaseResultUnknown
              ? '请使用重试确认原请求结果，避免重复操作。'
              : '将停止辅种并清理 PackBreaker 创建的链接与空目录；源媒体不会删除。'
          "
          type="warning"
          :closable="false"
          show-icon
          class="release-alert"
        >
          <template #default>
            <el-button size="small" type="warning" :loading="releasing" @click="releaseActiveTask">
              {{ releaseResultUnknown ? '重试确认释放结果' : '释放辅种资源' }}
            </el-button>
          </template>
        </el-alert>
        <el-alert
          v-if="canRerunActive"
          :title="rerunResultUnknown ? '重新运行结果未知' : '可重新运行任务'"
          :description="
            rerunResultUnknown
              ? '请使用重试确认原请求结果，避免重复创建。'
              : `当前 Run #${active.run_number} 保持原状态，新 Run 将重新处理。`
          "
          type="info"
          :closable="false"
          show-icon
          class="rerun-alert"
        >
          <template #default>
            <el-button size="small" type="primary" :loading="rerunning" @click="rerunActiveTask">
              {{
                rerunResultUnknown ? '重试确认重新运行结果' : `创建 Run #${active.run_number + 1}`
              }}
            </el-button>
          </template>
        </el-alert>
        <el-alert
          v-if="canPreCancelActive"
          :title="
            preCancelResultUnknown
              ? '取消结果未知'
              : activeCancellationIsCooperative
                ? '可停止当前分析'
                : '可取消当前任务'
          "
          :description="
            preCancelResultUnknown
              ? '请使用重试确认原请求结果，避免重复操作。'
              : activeCancellationIsCooperative
                ? '停止后已有媒体文件不会被修改。'
                : '取消后已有媒体文件不会被修改。'
          "
          type="warning"
          :closable="false"
          show-icon
          class="pre-cancel-alert"
        >
          <template #default>
            <el-button
              size="small"
              type="danger"
              :loading="preCancelling"
              @click="cancelBeforeSideEffects"
            >
              {{
                preCancelResultUnknown
                  ? '重试确认取消结果'
                  : activeCancellationIsCooperative
                    ? '请求停止只读分析'
                    : '取消未执行任务'
              }}
            </el-button>
          </template>
        </el-alert>
        <TaskAnalysisPanel :suggested-task-id="active.id" />
        <TaskOperationCenter :task-id="active.id" :refresh-key="operationRefreshKey" />
        <TaskEventTimeline
          :task-id="active.id"
          :live="detailVisible"
          @changed="handleTaskEventChanged"
        />
      </template>
    </el-drawer>
  </section>
</template>

<style scoped>
.real-task-center {
  margin-top: 0;
}
.real-task-filters {
  margin: 18px 0;
  grid-template-columns: minmax(280px, 1fr) 220px;
}
.real-task-identity,
.real-task-source {
  display: grid;
  gap: 5px;
}
.real-task-identity button {
  width: fit-content;
  border: 0;
  padding: 0;
  background: transparent;
  color: var(--blue);
  font-weight: 700;
  cursor: pointer;
}
.real-task-identity code,
.real-task-source code,
.real-task-center td code {
  overflow-wrap: anywhere;
  color: var(--muted);
  font-size: 10px;
}
.real-task-source b {
  font-size: 11px;
}
.real-task-center td small {
  display: block;
  margin-top: 5px;
  color: var(--muted);
  font-size: 10px;
}
.real-task-summary {
  margin-bottom: 18px;
}
.pre-cancel-alert {
  margin-bottom: 18px;
}
.release-alert,
.rerun-alert {
  margin-bottom: 18px;
}
.rerun-alert {
  margin-bottom: 18px;
}
@media (max-width: 800px) {
  .real-task-filters {
    grid-template-columns: 1fr;
  }
}
</style>
