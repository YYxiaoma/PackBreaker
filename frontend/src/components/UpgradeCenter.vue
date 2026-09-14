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
const dockerSocketCheck = computed(() =>
  report.value?.checks.find((check) => check.name === 'docker_socket'),
);

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
      '创建当前 SQLite 的一致性升级前备份并应用普通备份保留策略。不会访问 PT、下载器或媒体内容。主密钥 secret.key 仍需独立保管。',
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
          <h2>发布与升级 <small>真实本地预检 · 手工不可变镜像切换</small></h2>
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

      <el-alert
        class="section-space"
        title="页面预检只读取本地配置、数据库、主密钥与 /data 根状态，并故意跳过临时备份演练。正式升级前请运行维护 CLI 的完整 preflight。"
        type="info"
        :closable="false"
      />

      <div class="backup-card section-space">
        <DatabaseBackup :size="28" />
        <div class="backup-summary">
          <b>升级前一致性备份</b>
          <p>SQLite Backup API 快照；数据库中的凭证保持密文，secret.key 不在备份内。</p>
          <p v-if="latestBackupFile" class="green">本页最近创建：{{ latestBackupFile }}</p>
        </div>
        <el-button type="primary" :loading="backupLoading" @click="createUpgradeBackup">
          <DatabaseBackup :size="15" />创建升级前备份
        </el-button>
      </div>
    </section>

    <section class="panel">
      <h3>不可变镜像升级 runbook</h3>
      <el-alert
        :title="
          dockerSocketCheck?.code === 'DOCKER_SOCKET_PRESENT'
            ? '检测到 docker.sock，但 Web UI 仍不会调用 Docker API。'
            : '默认未挂载 docker.sock；升级由宿主机管理员显式执行。'
        "
        :type="dockerSocketCheck?.code === 'DOCKER_SOCKET_PRESENT' ? 'warning' : 'success'"
        :closable="false"
      />
      <ol class="upgrade-runbook section-space">
        <li>
          <b>验证发布资产</b>
          <p>核对 release manifest、SBOM 与 SHA256SUMS，并取得完整不可变镜像身份。</p>
          <code>&lt;registry&gt;/&lt;image&gt;@sha256:&lt;digest&gt;</code>
        </li>
        <li>
          <b>执行完整发布预检与备份</b>
          <code>python -m backend.app.maintenance preflight</code>
          <p>确认预检通过并独立保存 secret.key，再记录本次升级前备份文件。</p>
        </li>
        <li>
          <b>停止旧实例并切换 digest</b>
          <p>只在宿主机更新 Compose/image 引用到已验证 digest，然后启动新实例。</p>
        </li>
        <li>
          <b>验证新实例</b>
          <code>python -m backend.app.healthcheck</code>
          <p>确认 readiness、数据库 migration、secret 自检和后台 worker 均正常。</p>
        </li>
        <li>
          <b>失败时回滚</b>
          <p>
            停止新实例；使用升级前或 pre-upgrade 一致性备份恢复数据库，再用原不可变镜像 digest
            启动并重新验证 readiness。不要只把镜像 tag 改回旧值。
          </p>
        </li>
      </ol>

      <el-alert
        title="安全边界：本页面不会拉取镜像、修改 docker.sock、重建容器或在线替换数据库。目标 Docker 环境的真实升级/回滚仍必须按部署 runbook 验收。"
        type="warning"
        :closable="false"
      />
    </section>
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
.backup-summary p,
.upgrade-runbook p {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.backup-summary {
  min-width: 0;
  flex: 1;
}
.upgrade-runbook {
  display: grid;
  gap: 14px;
  padding-left: 22px;
}
.upgrade-runbook code {
  display: block;
  margin-top: 6px;
  overflow-wrap: anywhere;
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
