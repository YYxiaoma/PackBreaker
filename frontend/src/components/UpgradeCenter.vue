<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { ArrowUpCircle, DatabaseBackup, RefreshCw, ShieldCheck, TriangleAlert } from '@lucide/vue';
import { ElMessage, ElMessageBox } from 'element-plus';

import { toApiProblem } from '../api/client';
import {
  getReleasePreflight,
  getSystemUpgradeStatus,
  runBackupNow,
  startSystemUpgrade,
  type ReleasePreflight,
  type ReleasePreflightCheck,
  type SystemUpgradeStatus,
  type UpdaterStatus,
} from '../api/system';

const report = ref<ReleasePreflight>();
const upgrade = ref<SystemUpgradeStatus>();
const loading = ref(false);
const backupLoading = ref(false);
const upgradeLoading = ref(false);
const latestBackupFile = ref('');
const pendingIdempotencyKey = ref('');
const upgradeResultUnknown = ref(false);
let pollTimer: ReturnType<typeof setInterval> | undefined;

const activePhases = new Set<UpdaterStatus['phase']>([
  'accepted',
  'pulling',
  'stopping',
  'starting',
  'verifying',
  'rolling_back',
]);
const helperStatus = computed(() => upgrade.value?.helper_status ?? null);
const upgradeActive = computed(() =>
  helperStatus.value ? activePhases.has(helperStatus.value.phase) : false,
);
const overallType = computed(() => (report.value?.status === 'ready' ? 'success' : 'error'));
const canSubmitUpgrade = computed(
  () =>
    Boolean(upgrade.value?.latest_version && upgrade.value?.target_image_digest) &&
    !upgradeLoading.value &&
    !upgradeActive.value &&
    (upgrade.value?.can_upgrade === true || upgradeResultUnknown.value),
);

function checkType(status: ReleasePreflightCheck['status']): 'success' | 'warning' | 'danger' {
  return status === 'ok' ? 'success' : status === 'warning' ? 'warning' : 'danger';
}

function checkLabel(status: ReleasePreflightCheck['status']): string {
  return { ok: '通过', warning: '提示', blocked: '阻断' }[status];
}

function helperPhaseLabel(phase: UpdaterStatus['phase']): string {
  return {
    idle: '就绪',
    accepted: '已接管升级',
    pulling: '拉取镜像',
    stopping: '停止旧容器并备份',
    starting: '启动新容器',
    verifying: '健康检查',
    succeeded: '升级成功',
    rolling_back: '正在自动回滚',
    rolled_back: '已自动回滚',
    failed: '升级失败',
    manual_recovery_required: '需要人工恢复',
  }[phase];
}

function helperPhaseType(phase: UpdaterStatus['phase']): 'success' | 'warning' | 'danger' | 'info' {
  if (phase === 'succeeded') return 'success';
  if (phase === 'rolled_back' || phase === 'rolling_back') return 'warning';
  if (phase === 'failed' || phase === 'manual_recovery_required') return 'danger';
  return 'info';
}

function blockedReasonLabel(reason: string): string {
  return (
    {
      MAIN_DOCKER_SOCKET_PRESENT:
        '主 PackBreaker 容器仍挂载 docker.sock，请移除后仅交给 updater helper。',
      UPDATER_HELPER_UNAVAILABLE: '未检测到独立 updater helper，无法在主容器停机后继续升级。',
      UPDATER_BUSY: '独立 updater helper 正在处理升级任务。',
      UPDATER_MANUAL_RECOVERY_REQUIRED: '上一次升级现场需要人工核对，已禁止继续自动升级。',
      RELEASE_TARGET_UNAVAILABLE: '暂时无法取得正式发布目标。',
      CURRENT_VERSION_INVALID: '当前运行版本无法与正式版本进行安全比较。',
      NO_NEWER_RELEASE: '当前已经是最新正式版本。',
    }[reason] ?? reason
  );
}

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    const [preflight, upgradeStatus] = await Promise.all([
      getReleasePreflight(),
      getSystemUpgradeStatus(),
    ]);
    report.value = preflight;
    upgrade.value = upgradeStatus;
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    loading.value = false;
  }
}

async function refreshUpgradeQuietly(): Promise<void> {
  try {
    upgrade.value = await getSystemUpgradeStatus();
    const phase = upgrade.value.helper_status?.phase;
    if (phase && !activePhases.has(phase) && phase !== 'idle') {
      upgradeResultUnknown.value = false;
    }
  } catch {
    // 升级切换期间主容器会短暂离线；轮询保持安静，等待新容器恢复。
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

async function startUpgrade(): Promise<void> {
  const targetVersion = upgrade.value?.latest_version;
  const targetDigest = upgrade.value?.target_image_digest;
  if (!targetVersion || !targetDigest) return;

  if (!upgradeResultUnknown.value) {
    try {
      await ElMessageBox.confirm(
        `升级到 v${targetVersion}？PackBreaker 会先执行完整预检并创建一致性备份，然后由独立 updater helper 停止并重建主容器。新容器健康检查失败时会恢复旧镜像和切换瞬间数据库备份。升级期间页面会短暂断开。`,
        '确认 Docker 安全升级',
        {
          confirmButtonText: `升级到 v${targetVersion}`,
          cancelButtonText: '取消',
          type: 'warning',
        },
      );
    } catch {
      return;
    }
    pendingIdempotencyKey.value = `pb-upgrade-${crypto.randomUUID()}`;
  }

  const idempotencyKey = pendingIdempotencyKey.value;
  if (!idempotencyKey) return;
  upgradeLoading.value = true;
  try {
    const result = await startSystemUpgrade(
      {
        action: 'upgrade',
        target_version: targetVersion,
        target_image_digest: targetDigest,
      },
      idempotencyKey,
    );
    upgradeResultUnknown.value = false;
    latestBackupFile.value = result.backup_database_file ?? latestBackupFile.value;
    ElMessage.success(
      result.idempotency_replayed
        ? '原升级请求已确认，继续由 updater helper 执行。'
        : '升级已交给独立 updater helper；页面短暂断开属于正常现象。',
    );
    await refreshUpgradeQuietly();
  } catch (caught) {
    const problem = toApiProblem(caught);
    if (
      problem.code === 'API_UNAVAILABLE' ||
      problem.status === 408 ||
      (problem.status ?? 0) >= 500
    ) {
      upgradeResultUnknown.value = true;
      ElMessage.warning('升级请求结果暂时未知；请使用当前按钮以同一请求号重试确认，避免重复升级。');
    } else {
      pendingIdempotencyKey.value = '';
      upgradeResultUnknown.value = false;
      ElMessage.error(problem.message);
    }
  } finally {
    upgradeLoading.value = false;
  }
}

onMounted(() => {
  void refresh();
  pollTimer = setInterval(() => void refreshUpgradeQuietly(), 3000);
});
onUnmounted(() => {
  if (pollTimer !== undefined) clearInterval(pollTimer);
});
</script>

<template>
  <div class="upgrade-center" v-loading="loading">
    <section class="panel">
      <div class="upgrade-header">
        <div>
          <h2>发布与升级</h2>
          <p class="muted">
            当前运行版本
            <strong>v{{ upgrade?.current_version ?? report?.app_version ?? '—' }}</strong>
            <template v-if="upgrade?.latest_version">
              · 最新正式版本 <strong>v{{ upgrade.latest_version }}</strong>
            </template>
          </p>
        </div>
        <el-button @click="refresh"><RefreshCw :size="15" />刷新升级状态</el-button>
      </div>

      <div v-if="upgrade" class="release-card section-space">
        <div>
          <span class="muted">正式镜像</span>
          <code>{{ upgrade.immutable_image ?? '暂不可用' }}</code>
        </div>
        <el-tag :type="upgrade.update_available ? 'warning' : 'success'">
          {{ upgrade.update_available ? '发现新版本' : '已是最新版本' }}
        </el-tag>
      </div>

      <div v-if="helperStatus" class="helper-card section-space">
        <div>
          <b>独立 updater helper</b>
          <p>{{ helperStatus.message }}</p>
        </div>
        <el-tag :type="helperPhaseType(helperStatus.phase)">
          {{ helperPhaseLabel(helperStatus.phase) }}
        </el-tag>
      </div>

      <el-alert
        v-if="upgradeResultUnknown"
        class="section-space"
        title="上一次升级请求结果暂时未知"
        description="请使用下方按钮以同一请求号重试确认；不要刷新后重新发起另一条升级请求。"
        type="warning"
        :closable="false"
        show-icon
      />

      <div v-if="upgrade?.blocked_reasons?.length" class="blocked-list section-space">
        <el-alert
          v-for="reason in upgrade.blocked_reasons"
          :key="reason"
          :title="blockedReasonLabel(reason)"
          :type="reason === 'NO_NEWER_RELEASE' ? 'success' : 'warning'"
          :closable="false"
          show-icon
        />
      </div>

      <div class="upgrade-action section-space">
        <div>
          <b>Docker 安全升级</b>
          <p>
            由独立 helper 接管容器替换；主 PackBreaker 不需要也不应挂载
            docker.sock。失败时自动恢复旧镜像和切换瞬间数据库备份。
          </p>
        </div>
        <el-button
          type="warning"
          :loading="upgradeLoading || upgradeActive"
          :disabled="!canSubmitUpgrade"
          @click="startUpgrade"
        >
          <ArrowUpCircle :size="16" />
          {{
            upgradeResultUnknown
              ? '重试确认升级请求'
              : upgrade?.latest_version
                ? `升级到 v${upgrade.latest_version}`
                : '暂无升级目标'
          }}
        </el-button>
      </div>
    </section>

    <section class="panel">
      <h3>本地升级前置检查</h3>
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
          <b>手动创建一致性备份</b>
          <p>自动升级会强制创建备份；这里也可以单独创建。主密钥 secret.key 需要独立保管。</p>
          <p v-if="latestBackupFile" class="green">本页最近创建：{{ latestBackupFile }}</p>
        </div>
        <el-button type="primary" :loading="backupLoading" @click="createUpgradeBackup">
          <DatabaseBackup :size="15" />创建一致性备份
        </el-button>
      </div>
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
.backup-summary p {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.backup-summary {
  min-width: 0;
  flex: 1;
}
.release-card,
.helper-card,
.upgrade-action {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: 12px;
}
.release-card > div,
.helper-card > div,
.upgrade-action > div {
  min-width: 0;
}
.release-card code {
  display: block;
  margin-top: 5px;
  overflow-wrap: anywhere;
  font-size: 12px;
}
.helper-card p,
.upgrade-action p {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.blocked-list {
  display: grid;
  gap: 8px;
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
