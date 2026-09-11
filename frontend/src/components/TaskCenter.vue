<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { Plus, RefreshCw, Search } from '@lucide/vue';

import { ApiProblem } from '../api/client';
import {
  createTask,
  listTasks,
  type TaskCreateInput,
  type TaskRecord,
  type TaskStatus,
} from '../api/tasks';
import TaskAnalysisPanel from './TaskAnalysisPanel.vue';
import TaskEventTimeline from './TaskEventTimeline.vue';
import TaskOperationCenter from './TaskOperationCenter.vue';

const tasks = ref<TaskRecord[]>([]);
const loading = ref(false);
const createVisible = ref(false);
const detailVisible = ref(false);
const active = ref<TaskRecord | null>(null);
const operationRefreshKey = ref(0);
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
    ElMessage.success(result.created ? '真实任务已登记' : '相同任务已存在，已打开原任务');
  } catch (error) {
    showError(error);
  } finally {
    creating.value = false;
  }
}

function open(task: TaskRecord): void {
  active.value = task;
  detailVisible.value = true;
}

async function handleTaskEventChanged(): Promise<void> {
  operationRefreshKey.value += 1;
  await refresh();
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
      <h2>真实任务中心 <small>数据来自 SQLite `/tasks`，不使用 PB-* 合成任务</small></h2>
      <div>
        <el-button :loading="loading" @click="refresh"><RefreshCw :size="16" />刷新</el-button>
        <el-button type="primary" @click="createVisible = true"
          ><Plus :size="16" />登记任务</el-button
        >
      </div>
    </div>

    <el-alert
      title="任务登记与 Analyze 分离"
      description="创建任务只登记来源身份与 normalized unit key，不扫描磁盘、不搜索站点、不写下载器。打开任务后再显式提供 /data 相对 source_root 执行只读 Analyze。"
      type="info"
      :closable="false"
      show-icon
    />

    <div class="filters real-task-filters">
      <el-input v-model="query" clearable placeholder="搜索 UUID、source hash、unit key…">
        <template #prefix><Search :size="16" /></template>
      </el-input>
      <el-select v-model="status" clearable placeholder="全部状态">
        <el-option v-for="item in statusOptions" :key="item" :label="item" :value="item" />
      </el-select>
    </div>

    <el-table v-loading="loading" :data="filtered" row-key="id" empty-text="数据库中暂无真实任务">
      <el-table-column label="任务" min-width="290">
        <template #default="{ row }">
          <div class="real-task-identity">
            <button @click="open(row)">{{ row.type }}</button>
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

    <el-dialog v-model="createVisible" title="登记真实任务" width="620px">
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
      :title="active ? `真实任务 ${active.id}` : '真实任务'"
    >
      <template v-if="active">
        <el-descriptions :column="2" border class="real-task-summary">
          <el-descriptions-item label="状态">{{ active.status }}</el-descriptions-item>
          <el-descriptions-item label="版本">v{{ active.version }}</el-descriptions-item>
          <el-descriptions-item label="来源下载器">{{
            active.source_downloader_id
          }}</el-descriptions-item>
          <el-descriptions-item label="Source hash">{{ active.source_hash }}</el-descriptions-item>
          <el-descriptions-item label="Unit key" :span="2">{{
            active.normalized_unit_key
          }}</el-descriptions-item>
        </el-descriptions>
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
@media (max-width: 800px) {
  .real-task-filters {
    grid-template-columns: 1fr;
  }
}
</style>
