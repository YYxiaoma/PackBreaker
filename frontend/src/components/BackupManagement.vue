<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { DatabaseBackup, RefreshCw, ShieldCheck } from '@lucide/vue';
import { ElMessage, ElMessageBox } from 'element-plus';

import { toApiProblem } from '../api/client';
import {
  getBackupPolicy,
  runBackupNow,
  updateBackupPolicy,
  type BackupPolicy,
  type BackupPolicyUpdate,
} from '../api/system';

const policy = ref<BackupPolicy>();
const loading = ref(false);
const saving = ref(false);
const running = ref(false);
const draft = reactive<BackupPolicyUpdate>({
  enabled: false,
  interval_hours: 24,
  retention_days: 30,
  keep_latest: 3,
});

const driverTone = computed(() =>
  policy.value?.driver_running && !policy.value.driver_consecutive_errors ? 'success' : 'warning',
);

function syncDraft(value: BackupPolicy): void {
  draft.enabled = value.enabled;
  draft.interval_hours = value.interval_hours;
  draft.retention_days = value.retention_days;
  draft.keep_latest = value.keep_latest;
}

function formatTime(value: string | null | undefined): string {
  if (!value) return '尚无';
  return new Date(value).toLocaleString();
}

async function load(): Promise<void> {
  loading.value = true;
  try {
    const value = await getBackupPolicy();
    policy.value = value;
    syncDraft(value);
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    loading.value = false;
  }
}

async function save(): Promise<void> {
  if (!policy.value) return;
  if (draft.enabled && !policy.value.enabled) {
    try {
      await ElMessageBox.confirm(
        `启用后后台将每 ${draft.interval_hours} 小时检查并创建一致性数据库备份，同时按当前保留策略清理普通备份。不会访问 PT、下载器或媒体内容。`,
        '启用计划备份',
        {
          confirmButtonText: '确认启用',
          cancelButtonText: '取消',
          type: 'warning',
        },
      );
    } catch {
      return;
    }
  }

  saving.value = true;
  try {
    const updated = await updateBackupPolicy(policy.value.version, { ...draft });
    policy.value = updated;
    syncDraft(updated);
    ElMessage.success(
      updated.enabled ? '计划备份策略已保存并启用' : '计划备份策略已保存；自动执行保持关闭',
    );
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
    await load();
  } finally {
    saving.value = false;
  }
}

async function runNow(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '立即创建当前 SQLite 的一致性快照，并应用当前备份保留策略。此动作不暂停任务、不访问 PT/下载器，也不读取媒体内容。',
      '立即创建备份',
      {
        confirmButtonText: '创建一致性备份',
        cancelButtonText: '取消',
        type: 'warning',
      },
    );
  } catch {
    return;
  }

  running.value = true;
  try {
    const result = await runBackupNow();
    if (!result.created) {
      ElMessage.info(`本次未创建备份：${result.skipped_reason ?? '未知原因'}`);
    } else if (result.retention_error_code) {
      ElMessage.warning(
        `备份 ${result.database_file ?? ''} 已创建，但保留策略需要关注：${result.retention_error_code}`,
      );
    } else {
      ElMessage.success(`一致性备份已创建：${result.database_file ?? 'packbreaker backup'}`);
    }
    await load();
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    running.value = false;
  }
}

onMounted(() => void load());
</script>

<template>
  <div v-loading="loading" class="backup-management">
    <el-alert
      title="计划备份默认关闭。备份包含数据库中的加密凭证记录，但不包含 secret.key；主密钥必须独立安全保管。"
      type="warning"
      :closable="false"
      show-icon
    />

    <div class="backup-card section-space">
      <DatabaseBackup :size="30" />
      <div class="backup-summary">
        <b>SQLite 一致性备份</b>
        <p>
          {{ policy?.enabled ? `每 ${policy.interval_hours} 小时自动检查` : '自动计划当前关闭' }} ·
          最近成功 {{ formatTime(policy?.last_success_at) }}
        </p>
      </div>
      <el-button type="primary" :loading="running" :disabled="!policy" @click="runNow">
        <DatabaseBackup :size="15" />立即备份
      </el-button>
    </div>

    <div v-if="policy" class="setting-row">
      <div>
        <b>备份后台 Driver</b>
        <p>
          运行状态只负责本地数据库快照与保留清理；连续错误 {{ policy.driver_consecutive_errors }} 次
        </p>
      </div>
      <el-tag :type="driverTone">{{ policy.driver_running ? '运行中' : '未运行' }}</el-tag>
    </div>

    <el-form v-if="policy" label-position="top" class="section-space">
      <div class="form-grid">
        <el-form-item label="计划备份">
          <el-switch v-model="draft.enabled" active-text="启用" inactive-text="关闭" />
        </el-form-item>
        <el-form-item label="备份周期（小时）">
          <el-input-number v-model="draft.interval_hours" :min="1" :max="168" />
        </el-form-item>
        <el-form-item label="普通备份保留（天）">
          <el-input-number v-model="draft.retention_days" :min="1" :max="3650" />
        </el-form-item>
        <el-form-item label="至少保留最新份数">
          <el-input-number v-model="draft.keep_latest" :min="1" :max="100" />
        </el-form-item>
      </div>
      <div class="backup-actions">
        <el-button type="primary" :loading="saving" @click="save">保存备份策略</el-button>
        <el-button :disabled="loading" @click="load"><RefreshCw :size="15" />刷新状态</el-button>
      </div>
    </el-form>

    <div v-if="policy" class="health-list section-space">
      <div>
        <ShieldCheck :size="17" />最近尝试
        <span>{{ formatTime(policy.last_attempt_at) }}</span>
      </div>
      <div>
        <ShieldCheck :size="17" />最近成功
        <span>{{ formatTime(policy.last_success_at) }}</span>
      </div>
      <div>
        <ShieldCheck :size="17" />最近错误
        <el-tag :type="policy.last_error_code ? 'warning' : 'success'">
          {{ policy.last_error_code ?? '无' }}
        </el-tag>
      </div>
    </div>

    <el-alert
      class="section-space"
      title="在线管理只允许备份与策略配置。数据库恢复必须停止活动 PackBreaker 实例后使用维护 CLI 执行；管理页面不会提供在线替换数据库按钮。"
      type="info"
      :closable="false"
    />
  </div>
</template>

<style scoped>
.backup-management {
  display: grid;
  gap: 14px;
}
.backup-summary {
  min-width: 0;
  flex: 1;
}
.backup-summary p {
  margin: 5px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.backup-actions {
  display: flex;
  gap: 8px;
}
.health-list span {
  margin-left: auto;
  color: var(--muted);
  font-size: 12px;
}
@media (max-width: 800px) {
  .backup-card {
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .backup-card .el-button {
    width: 100%;
  }
}
</style>
