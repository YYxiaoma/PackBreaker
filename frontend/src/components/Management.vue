<script setup lang="ts">
import { ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import {
  HardDrive,
  ShieldCheck,
  Check,
  RefreshCw,
  AlertTriangle,
  Server,
  Bell,
  RotateCcw,
} from '@lucide/vue';
import { ApiProblem } from '../api/client';
import {
  getOperationMaintenanceReport,
  getOperationRetentionPlan,
  purgeTaskOperation,
  type OperationMaintenanceReport,
  type OperationRetentionPlan,
} from '../api/tasks';
import { createTaskActionIdempotencyKey } from '../taskActionSafety';
import DownloaderManagement from './DownloaderManagement.vue';
import SiteManagement from './SiteManagement.vue';
import NotificationManagement from './NotificationManagement.vue';
import OperationalLogs from './OperationalLogs.vue';
import BackupManagement from './BackupManagement.vue';
const props = defineProps<{ page: string }>();
const maintenanceReport = ref<OperationMaintenanceReport>();
const maintenanceLoading = ref(false);
const retentionPlan = ref<OperationRetentionPlan>();
const retentionDays = ref(30);
const retentionLoading = ref(false);
const purgingJournalId = ref('');
const retentionResultUnknown = ref(false);
const pendingRetentionPurge = ref<{
  taskId: string;
  journalId: string;
  retentionDays: number;
  idempotencyKey: string;
}>();
async function refreshMaintenanceReport() {
  maintenanceLoading.value = true;
  try {
    maintenanceReport.value = await getOperationMaintenanceReport(100);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '清理/对账报告读取失败');
  } finally {
    maintenanceLoading.value = false;
  }
}
async function refreshRetentionPlan() {
  retentionLoading.value = true;
  try {
    retentionPlan.value = await getOperationRetentionPlan(retentionDays.value, 100);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '保留期安全预览读取失败');
  } finally {
    retentionLoading.value = false;
  }
}
async function refreshMaintenanceCenter() {
  await Promise.all([refreshMaintenanceReport(), refreshRetentionPlan()]);
}
function isUnknownMutationResult(error: unknown): boolean {
  return (
    error instanceof ApiProblem &&
    (error.code === 'API_UNAVAILABLE' || error.status === 408 || (error.status ?? 0) >= 500)
  );
}
function retentionReasonLabel(reason: string): string {
  return (
    {
      ELIGIBLE: '可安全清理',
      RETENTION_WINDOW_NOT_REACHED: '未到保留期',
      TASK_NOT_TERMINAL: '任务未终态',
      TASK_CHECKPOINT_REFERENCE: '任务仍在使用该记录',
      ACTION_RECEIPT_REFERENCE: '关联操作仍在使用该记录',
      JOURNAL_REFERENCE: '其他 journal 仍引用',
    }[reason] ?? reason
  );
}
async function purgeRetentionItem(item: OperationRetentionPlan['items'][number]) {
  const planRetentionDays = retentionPlan.value?.retention_days;
  if (!item.eligible || purgingJournalId.value || planRetentionDays === undefined) return;
  try {
    await ElMessageBox.confirm(
      `清理 Journal ${item.journal_id} 的敏感 operation payload？服务端会再次验证任务终态、${planRetentionDays} 天保留期与零恢复引用。此动作不会删除媒体文件、下载器任务或其他外部资源，并会保留最小 tombstone 阻止历史幂等键再次执行。`,
      '确认清理历史操作记录',
      {
        confirmButtonText: '重新验证并清理 payload',
        cancelButtonText: '返回',
        type: 'warning',
      },
    );
  } catch {
    return;
  }
  pendingRetentionPurge.value = {
    taskId: item.task_id,
    journalId: item.journal_id,
    retentionDays: planRetentionDays,
    idempotencyKey: createTaskActionIdempotencyKey('purge', item.task_id),
  };
  retentionResultUnknown.value = false;
  await executePendingRetentionPurge();
}
async function executePendingRetentionPurge() {
  const pending = pendingRetentionPurge.value;
  if (!pending || purgingJournalId.value) return;
  purgingJournalId.value = pending.journalId;
  try {
    const result = await purgeTaskOperation(
      pending.taskId,
      pending.journalId,
      pending.retentionDays,
      pending.idempotencyKey,
    );
    pendingRetentionPurge.value = undefined;
    retentionResultUnknown.value = false;
    ElMessage.success(
      `Journal payload 已安全清理${result.idempotency_replayed ? '（幂等重放确认）' : ''}；最小 tombstone 已保留`,
    );
    await refreshMaintenanceCenter();
  } catch (error) {
    if (isUnknownMutationResult(error)) {
      retentionResultUnknown.value = true;
      ElMessage.warning('清理响应结果未知；请使用当前操作继续重试确认');
    } else {
      pendingRetentionPurge.value = undefined;
      retentionResultUnknown.value = false;
      ElMessage.error(error instanceof Error ? error.message : '历史操作记录清理失败');
      await refreshRetentionPlan();
    }
  } finally {
    purgingJournalId.value = '';
  }
}
watch(
  () => props.page,
  (page) => {
    if (page === '清理与对账') void refreshMaintenanceCenter();
  },
  { immediate: true },
);
const settingTab = ref('通知');
</script>
<template>
  <OperationalLogs v-if="page === '日志'" />
  <DownloaderManagement v-else-if="page === '下载器'" />
  <SiteManagement v-else-if="page === '站点管理'" />
  <div v-else-if="page === '清理与对账'">
    <div class="settings-layout">
      <section class="panel">
        <h3>清理 / 对账报告</h3>
        <div class="health-list">
          <div>
            <Server :size="18" />操作记录
            <el-tag type="info">{{ maintenanceReport?.summary.total_journals ?? 0 }} 项</el-tag>
          </div>
          <div>
            <AlertTriangle :size="18" />需要关注
            <el-tag type="danger"
              >{{ maintenanceReport?.summary.attention_required ?? 0 }} 项</el-tag
            >
          </div>
          <div>
            <RefreshCw :size="18" />可只读对账
            <el-tag type="warning"
              >{{ maintenanceReport?.summary.reconcile_supported ?? 0 }} 项</el-tag
            >
          </div>
          <div>
            <ShieldCheck :size="18" />仅人工检查
            <el-tag>{{ maintenanceReport?.summary.manual_only ?? 0 }} 项</el-tag>
          </div>
        </div>
        <el-button type="primary" :loading="maintenanceLoading" @click="refreshMaintenanceReport">
          <RefreshCw :size="15" />刷新报告
        </el-button>
      </section>
      <section class="panel">
        <h3>保留期清理候选</h3>
        <div class="cleanup-value">
          {{ maintenanceReport?.summary.retention_candidates ?? 0 }} <span>个清理候选</span>
        </div>
        <div class="filters section-space">
          <el-input-number v-model="retentionDays" :min="1" :max="3650" :step="1" />
          <span class="muted">天保留期</span>
          <el-button :loading="retentionLoading" @click="refreshRetentionPlan">
            <RefreshCw :size="15" />重新生成安全预览
          </el-button>
        </div>
        <div class="health-list section-space">
          <div>
            <Server :size="18" />已检查
            <el-tag type="info">{{ retentionPlan?.summary.inspected ?? 0 }} 项</el-tag>
          </div>
          <div>
            <Check :size="18" />可安全清理
            <el-tag type="success">{{ retentionPlan?.summary.eligible ?? 0 }} 项</el-tag>
          </div>
          <div>
            <ShieldCheck :size="18" />安全阻断
            <el-tag type="warning">{{ retentionPlan?.summary.blocked ?? 0 }} 项</el-tag>
          </div>
        </div>
        <el-alert
          title="清理只移除 PackBreaker 操作记录，不会删除媒体文件或下载器任务。"
          type="info"
          :closable="false"
          show-icon
        />
      </section>
    </div>

    <el-alert
      v-if="maintenanceReport?.summary.truncated"
      class="section-space"
      title="报告结果已截断"
      description="当前只展示最早更新的 100 条修复项和 100 条保留期候选；汇总计数仍为全量。"
      type="warning"
      :closable="false"
      show-icon
    />

    <el-alert
      v-if="retentionPlan?.summary.truncated"
      class="section-space"
      title="保留期安全预览已截断"
      description="当前仅检查最早更新的 100 条候选。"
      type="warning"
      :closable="false"
      show-icon
    />

    <el-alert
      v-if="retentionResultUnknown && pendingRetentionPurge"
      class="section-space"
      title="上一次清理结果未知"
      type="warning"
      :closable="false"
      show-icon
    >
      <template #default>
        <p>
          任务 {{ pendingRetentionPurge.taskId }} · 记录
          {{ pendingRetentionPurge.journalId }}。请使用下方按钮确认原请求结果，避免重复清理。
        </p>
        <el-button
          type="warning"
          :loading="purgingJournalId === pendingRetentionPurge.journalId"
          @click="executePendingRetentionPurge"
        >
          重试确认同一清理请求
        </el-button>
      </template>
    </el-alert>

    <section class="panel section-space">
      <h3>保留期安全预览</h3>
      <el-empty
        v-if="!retentionLoading && !retentionPlan?.items.length"
        description="当前没有可清理的操作记录"
      />
      <div v-for="item in retentionPlan?.items ?? []" :key="item.journal_id" class="resource-row">
        <span class="file-icon" :class="{ success: item.eligible, warning: !item.eligible }">
          <Check v-if="item.eligible" :size="20" />
          <ShieldCheck v-else :size="20" />
        </span>
        <div>
          <b>{{ item.kind }} · {{ item.status }}</b>
          <p>{{ retentionReasonLabel(item.reason_code) }}</p>
          <small class="muted">任务 {{ item.task_id }} · 记录 {{ item.journal_id }}</small>
        </div>
        <el-button
          v-if="item.eligible"
          type="danger"
          plain
          :disabled="Boolean(purgingJournalId) || retentionResultUnknown"
          :loading="purgingJournalId === item.journal_id"
          @click="purgeRetentionItem(item)"
        >
          清理 payload
        </el-button>
        <el-tag v-else type="warning">{{ retentionReasonLabel(item.reason_code) }}</el-tag>
      </div>
    </section>

    <section class="panel section-space">
      <h3>人工修复清单</h3>
      <el-empty
        v-if="!maintenanceLoading && !maintenanceReport?.repair_items.length"
        description="当前没有需要人工处理的操作记录"
      />
      <div
        v-for="item in maintenanceReport?.repair_items ?? []"
        :key="item.journal_id"
        class="resource-row"
      >
        <span class="file-icon" :class="{ warning: item.manual_required }">
          <AlertTriangle v-if="item.manual_required" :size="20" />
          <RefreshCw v-else :size="20" />
        </span>
        <div>
          <b>{{ item.kind }} · {{ item.status }}</b>
          <p>{{ item.reason }}</p>
          <small class="muted">任务 {{ item.task_id }} · 记录 {{ item.journal_id }}</small>
          <p>
            <small>{{ item.recommended_action }}</small>
          </p>
        </div>
        <el-tag :type="item.action === 'RECONCILE' ? 'warning' : 'danger'">
          {{ item.action === 'RECONCILE' ? '到任务详情对账' : '必须人工检查' }}
        </el-tag>
      </div>
    </section>

    <section class="panel section-space">
      <h3>维护报告候选概览</h3>
      <el-empty
        v-if="!maintenanceLoading && !maintenanceReport?.cleanup_candidates.length"
        description="当前没有清理候选"
      />
      <div
        v-for="item in maintenanceReport?.cleanup_candidates ?? []"
        :key="item.journal_id"
        class="resource-row"
      >
        <span class="file-icon success"><Check :size="20" /></span>
        <div>
          <b>{{ item.kind }} · {{ item.status }}</b>
          <p>{{ item.reason }}</p>
          <small class="muted">任务 {{ item.task_id }} · 记录 {{ item.journal_id }}</small>
          <p>
            <small>{{ item.recommendation }}</small>
          </p>
        </div>
        <el-tag type="info">候选概览</el-tag>
      </div>
    </section>
  </div>
  <div v-else-if="page === '系统设置'" class="panel">
    <el-tabs v-model="settingTab"
      ><el-tab-pane v-for="s in ['通知', '备份恢复']" :key="s" :name="s" :label="s"
    /></el-tabs>
    <div v-if="settingTab === '通知'" class="settings-content">
      <NotificationManagement />
    </div>
    <div v-else class="settings-content">
      <h3>备份与恢复</h3>
      <BackupManagement />
    </div>
  </div>
</template>
