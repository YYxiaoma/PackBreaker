<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { DatabaseBackup, RefreshCw, ShieldCheck, TriangleAlert } from '@lucide/vue';
import { ElMessage, ElMessageBox } from 'element-plus';

import { toApiProblem } from '../api/client';
import {
  getReleasePreflight,
  runBackupNow,
  type ReleasePreflight,
  type ReleasePreflightCheck,
} from '../api/system';

const report = ref<ReleasePreflight>();
const loading = ref(false);
const backupLoading = ref(false);
const latestBackupFile = ref('');

const overallType = computed(() => (report.value?.status === 'ready' ? 'success' : 'error'));

function checkType(status: ReleasePreflightCheck['status']): 'success' | 'warning' | 'danger' {
  return status === 'ok' ? 'success' : status === 'warning' ? 'warning' : 'danger';
}

function checkLabel(status: ReleasePreflightCheck['status']): string {
  return { ok: '通过', warning: '提示', blocked: '阻断' }[status];
}

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    report.value = await getReleasePreflight();
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    loading.value = false;
  }
}

async function createUpgradeBackup(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '创建升级前备份？主密钥 secret.key 需要独立保管。',
      '创建升级前备份',
      {
        confirmButtonText: '创建一致性备份',
        cancelButtonText: '取消',
        type: 'warning',
      },
    );
  } catch {
    return;
  }

  backupLoading.value = true;
  try {
    const result = await runBackupNow();
    if (!result.created) {
      ElMessage.info(`本次未创建备份：${result.skipped_reason ?? '未知原因'}`);
      return;
    }
    latestBackupFile.value = result.database_file ?? '';
    if (result.retention_error_code) {
      ElMessage.warning(
        `升级前备份 ${result.database_file ?? ''} 已创建，但保留策略需要关注：${result.retention_error_code}`,
      );
    } else {
      ElMessage.success(`升级前一致性备份已创建：${result.database_file ?? 'packbreaker backup'}`);
    }
    await refresh();
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    backupLoading.value = false;
  }
}

onMounted(() => void refresh());
</script>

<template>
  <div class="upgrade-center" v-loading="loading">
    <section class="panel">
      <div class="upgrade-header">
        <div>
          <h2>发布与升级</h2>
          <p class="muted">
            当前运行版本 <strong>v{{ report?.app_version ?? '—' }}</strong>
          </p>
        </div>
        <el-button @click="refresh"><RefreshCw :size="15" />刷新本地预检</el-button>
      </div>

      <el-alert
        v-if="report"
        :title="
          report.status === 'ready' ? '本地升级前置条件可继续检查' : '本地升级前置条件存在阻断项'
        "
        :type="overallType"
        :closable="false"
        show-icon
      />

      <div v-if="report" class="preflight-list section-space">
        <div v-for="check in report.checks" :key="check.name" class="preflight-row">
          <ShieldCheck v-if="check.status === 'ok'" :size="17" />
          <TriangleAlert v-else :size="17" />
          <div>
            <b>{{ check.code }}</b>
            <p>{{ check.detail }}</p>
          </div>
          <el-tag :type="checkType(check.status)">{{ checkLabel(check.status) }}</el-tag>
        </div>
      </div>

      <div class="backup-card section-space">
        <DatabaseBackup :size="28" />
        <div class="backup-summary">
          <b>升级前一致性备份</b>
          <p>主密钥 secret.key 不包含在备份中。</p>
          <p v-if="latestBackupFile" class="green">本页最近创建：{{ latestBackupFile }}</p>
        </div>
        <el-button type="primary" :loading="backupLoading" @click="createUpgradeBackup">
          <DatabaseBackup :size="15" />创建升级前备份
        </el-button>
      </div>
    </section>

    <el-alert title="执行升级前请确认备份与部署环境。" type="warning" :closable="false" />
  </div>
</template>

<style scoped>
.upgrade-center {
  display: grid;
  gap: 16px;
}
.upgrade-header {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
}
.upgrade-header h2 {
  margin: 0;
}
.upgrade-header h2 small {
  font-weight: 400;
  color: var(--muted);
}
.preflight-list {
  display: grid;
  gap: 8px;
}
.preflight-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
}
.preflight-row p,
.backup-summary p {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.backup-summary {
  min-width: 0;
  flex: 1;
}
@media (max-width: 800px) {
  .upgrade-header,
  .backup-card {
    align-items: stretch;
    flex-direction: column;
  }
  .preflight-row {
    grid-template-columns: auto minmax(0, 1fr);
  }
  .preflight-row .el-tag {
    grid-column: 2;
    justify-self: start;
  }
  .backup-card .el-button,
  .upgrade-header .el-button {
    width: 100%;
  }
}
</style>
