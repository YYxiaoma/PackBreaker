<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';

import { ApiProblem } from '../api/client';
import {
  listTaskOperations,
  reconcileTaskOperation,
  type TaskOperation,
  type TaskOperationReconcileAction,
} from '../api/tasks';
import { createTaskActionIdempotencyKey } from '../taskActionSafety';

const props = withDefaults(defineProps<{ taskId: string; refreshKey?: number }>(), {
  refreshKey: 0,
});

const operations = ref<TaskOperation[]>([]);
const loading = ref(false);
const reconciling = ref(false);
const errorCode = ref<string | null>(null);
const pendingJournalId = ref('');
const pendingIdempotencyKey = ref('');
const resultUnknown = ref(false);
const lastAction = ref<TaskOperationReconcileAction | null>(null);

const attentionCount = computed(
  () => operations.value.filter((item) => item.attention_required).length,
);

watch(
  () => [props.taskId, props.refreshKey] as const,
  (current, previous) => {
    if (!previous || current[0] !== previous[0]) clearPending();
    void refresh();
  },
  { immediate: true },
);

async function refresh(): Promise<void> {
  if (!props.taskId.trim()) return;
  loading.value = true;
  try {
    operations.value = await listTaskOperations(props.taskId);
    errorCode.value = null;
  } catch (error) {
    errorCode.value = error instanceof ApiProblem ? error.code : 'API_REQUEST_FAILED';
  } finally {
    loading.value = false;
  }
}

function canReplay(item: TaskOperation): boolean {
  return (
    resultUnknown.value &&
    pendingJournalId.value === item.id &&
    pendingIdempotencyKey.value.length > 0
  );
}

function canReconcile(item: TaskOperation): boolean {
  return (item.reconcile_supported || canReplay(item)) && !reconciling.value;
}

async function reconcile(item: TaskOperation): Promise<void> {
  if (!canReconcile(item)) return;
  const replaying = canReplay(item);
  reconciling.value = true;
  try {
    if (!replaying) {
      try {
        await ElMessageBox.confirm(
          '只重新读取当前文件系统对象或 qBittorrent torrent 状态，并用 operation journal 已登记的完成证据重新证明结果。此动作不会创建、删除、重命名、覆盖媒体文件，也不会重发 qB add/recheck/start/remove/stop；证据不匹配时保持安全阻断。',
          '重新验证 operation journal 证据',
          {
            confirmButtonText: '重新验证证据',
            cancelButtonText: '返回',
            type: 'warning',
          },
        );
      } catch {
        return;
      }
      pendingJournalId.value = item.id;
      pendingIdempotencyKey.value = createTaskActionIdempotencyKey('reconcile', props.taskId);
    }

    const result = await reconcileTaskOperation(
      props.taskId,
      pendingJournalId.value,
      pendingIdempotencyKey.value,
    );
    lastAction.value = result;
    clearPending();
    await refresh();
    ElMessage.success(
      `证据重新验证完成：${result.status}${result.idempotency_replayed ? '（幂等重放）' : ''}`,
    );
  } catch (error) {
    if (isUnknownMutationResult(error) && pendingJournalId.value && pendingIdempotencyKey.value) {
      resultUnknown.value = true;
      ElMessage.warning('对账响应结果未知；只能使用同一 journal 与同一 Idempotency-Key 重试确认');
    } else {
      clearPending();
    }
    showError(error);
  } finally {
    reconciling.value = false;
  }
}

function clearPending(): void {
  pendingJournalId.value = '';
  pendingIdempotencyKey.value = '';
  resultUnknown.value = false;
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
  ElMessage.error(error instanceof Error ? error.message : 'operation journal 请求失败');
}

function statusType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (['APPLIED', 'ROLLED_BACK', 'NOOP'].includes(status)) return 'success';
  if (['RECONCILE_REQUIRED', 'ROLLBACK_BLOCKED'].includes(status)) return 'danger';
  if (['INTENT_RECORDED', 'ROLLBACK_PENDING'].includes(status)) return 'warning';
  return 'info';
}

function kindLabel(kind: string): string {
  const labels: Record<string, string> = {
    FILESYSTEM_DIRECTORY: '文件目录',
    FILESYSTEM_HARDLINK: '硬链接',
    QBITTORRENT_ADD: 'qB 添加',
    QBITTORRENT_RECHECK: 'qB 校验',
    QBITTORRENT_START: 'qB 启动作种',
    QBITTORRENT_REMOVE: 'qB 移除',
    OTHER: '其他受控操作',
  };
  return labels[kind] ?? '其他受控操作';
}
</script>

<template>
  <section class="task-operation-center">
    <div class="task-operation-heading">
      <div>
        <h3>阻断 / 对账操作中心</h3>
        <small>
          仅展示 journal ID、固定操作类别、状态与时间；不返回路径、hash、save path、快照、ownership
          tag 或幂等键。
        </small>
      </div>
      <div class="task-operation-heading-actions">
        <el-tag v-if="attentionCount" type="danger">{{ attentionCount }} 项需关注</el-tag>
        <el-button size="small" :loading="loading" @click="refresh">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="errorCode"
      :title="`operation journal 暂不可用：${errorCode}`"
      type="warning"
      :closable="false"
      show-icon
    />
    <el-alert
      title="自动对账范围严格受限"
      description="目录/硬链接仅在存在 after snapshot 且当前对象完全匹配时重新确认；qB ADD/RECHECK/START 与 Transmission ADD/VERIFY/START/REMOVE 也只在历史完成证据、下载器配置版本和当前真实状态共同满足对应后置条件时允许只读重验。qB REMOVE、无完成快照的未知结果与 ROLLBACK_BLOCKED 仍只读展示。"
      type="info"
      :closable="false"
      show-icon
    />

    <el-empty
      v-if="!loading && !operations.length"
      description="当前任务暂无 operation journal"
      :image-size="56"
    />
    <el-table v-else v-loading="loading" :data="operations" row-key="id" size="small">
      <el-table-column label="操作" min-width="150">
        <template #default="{ row }">
          <b>{{ kindLabel(row.kind) }}</b>
          <small
            ><code>{{ row.id }}</code></small
          >
        </template>
      </el-table-column>
      <el-table-column label="状态" width="190">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)">{{ row.status }}</el-tag>
          <small v-if="row.attention_required" class="operation-attention">需要安全对账</small>
        </template>
      </el-table-column>
      <el-table-column label="更新时间" width="180">
        <template #default="{ row }">
          <small>{{ new Date(row.updated_at).toLocaleString() }}</small>
        </template>
      </el-table-column>
      <el-table-column label="安全动作" min-width="180">
        <template #default="{ row }">
          <el-button
            v-if="row.reconcile_supported || canReplay(row)"
            size="small"
            type="warning"
            :loading="reconciling && pendingJournalId === row.id"
            :disabled="!canReconcile(row)"
            @click="reconcile(row)"
          >
            {{ canReplay(row) ? '重试确认对账结果' : '重新验证证据' }}
          </el-button>
          <small v-else-if="row.attention_required">当前状态没有可自动证明的安全恢复动作</small>
          <small v-else>无需人工动作</small>
        </template>
      </el-table-column>
    </el-table>

    <el-alert
      v-if="lastAction"
      :title="`最近对账：${lastAction.kind} → ${lastAction.status}`"
      :description="
        lastAction.idempotency_replayed
          ? '结果由同一幂等请求重放确认。'
          : '当前资源已重新证明原 journal 完成证据。'
      "
      type="success"
      :closable="false"
      show-icon
    />
  </section>
</template>

<style scoped>
.task-operation-center {
  display: grid;
  gap: 12px;
  margin: 18px 0;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface);
}
.task-operation-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.task-operation-heading h3 {
  margin: 0 0 4px;
}
.task-operation-heading small,
.task-operation-center td small {
  display: block;
  margin-top: 4px;
  color: var(--muted);
  font-size: 11px;
}
.task-operation-heading-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.operation-attention {
  color: var(--red) !important;
}
.task-operation-center code {
  overflow-wrap: anywhere;
}
@media (max-width: 720px) {
  .task-operation-heading {
    flex-direction: column;
  }
}
</style>
