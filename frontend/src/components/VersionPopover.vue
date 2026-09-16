<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import {
  ArrowUpCircle,
  Check,
  ChevronDown,
  Copy,
  ExternalLink,
  GitBranch,
  RefreshCw,
  RotateCcw,
  Terminal,
  TriangleAlert,
} from '@lucide/vue';
import { ElMessage, ElMessageBox } from 'element-plus';

import { toApiProblem } from '../api/client';
import {
  getSystemHealth,
  getSystemUpgradeStatus,
  startSystemUpgrade,
  type SystemUpgradeStatus,
  type UpdaterStatus,
} from '../api/system';

const visible = ref(false);
const currentVersion = ref('—');
const upgrade = ref<SystemUpgradeStatus>();
const releaseLoading = ref(false);
const releaseChecked = ref(false);
const upgradeLoading = ref(false);
const upgradeResultUnknown = ref(false);
const pendingIdempotencyKey = ref('');
const targetInFlight = ref('');
const manualOpen = ref(false);
const rollbackOpen = ref(false);
let pollTimer: ReturnType<typeof setInterval> | undefined;

const activePhases = new Set<UpdaterStatus['phase']>([
  'accepted',
  'pulling',
  'stopping',
  'starting',
  'verifying',
  'rolling_back',
]);

const displayedVersion = computed(() => upgrade.value?.current_version || currentVersion.value);
const releaseLookupFailed = computed(() => Boolean(upgrade.value?.release_error_code));
const latestVersion = computed(() => upgrade.value?.latest_version ?? null);
const targetImage = computed(() => upgrade.value?.immutable_image ?? null);
const upgradeActive = computed(() => {
  const phase = upgrade.value?.helper_status?.phase;
  return phase ? activePhases.has(phase) : false;
});
const canAutoUpgrade = computed(
  () =>
    Boolean(upgrade.value?.latest_version && upgrade.value?.target_image_digest) &&
    !upgradeLoading.value &&
    !upgradeActive.value &&
    (upgrade.value?.can_upgrade === true || upgradeResultUnknown.value),
);
const automaticUpgradeHint = computed(() => {
  const reasons = upgrade.value?.blocked_reasons ?? [];
  if (reasons.includes('UPDATER_DOCKER_SOCKET_REQUIRED')) {
    return '单容器一键升级需要挂载 /var/run/docker.sock。';
  }
  if (reasons.includes('UPDATER_DOCKER_UNAVAILABLE')) {
    return '当前 PackBreaker 无法访问 Docker Engine，请检查 docker.sock 权限。';
  }
  if (reasons.includes('UPDATER_MANUAL_RECOVERY_REQUIRED')) {
    return '上一次升级现场需要人工核对，当前禁止继续自动升级。';
  }
  if (reasons.includes('UPDATER_BUSY')) return '已有升级正在执行。';
  if (upgrade.value?.update_available && upgrade.value?.can_upgrade === false) {
    return '当前部署条件暂不满足一键升级，可展开高级手动升级。';
  }
  return '';
});
const helperPhaseText = computed(() => {
  const phase = upgrade.value?.helper_status?.phase;
  if (!phase) return '';
  return {
    idle: '准备就绪',
    accepted: '已接管升级',
    pulling: '正在拉取镜像',
    stopping: '正在停止旧容器并备份',
    starting: '正在启动新容器',
    verifying: '正在执行健康检查',
    succeeded: '升级成功',
    rolling_back: '正在自动回滚',
    rolled_back: '已自动回滚',
    failed: '升级失败',
    manual_recovery_required: '需要人工恢复',
  }[phase];
});
const releaseUrl = computed(() => {
  const tag = upgrade.value?.target_tag;
  return tag
    ? `https://github.com/YYxiaoma/PackBreaker/releases/tag/${encodeURIComponent(tag)}`
    : 'https://github.com/YYxiaoma/PackBreaker/releases';
});
const statusKind = computed<'idle' | 'latest' | 'update' | 'error'>(() => {
  if (!releaseChecked.value) return 'idle';
  if (releaseLookupFailed.value || !latestVersion.value) return 'error';
  return upgrade.value?.update_available ? 'update' : 'latest';
});
const statusText = computed(() => {
  if (releaseLoading.value) return '正在检查正式版本…';
  if (statusKind.value === 'error') return '无法检查最新版本';
  if (statusKind.value === 'update') return `发现新版本 v${latestVersion.value}`;
  if (statusKind.value === 'latest') return '已是最新版本';
  return '点击刷新检查正式版本';
});
const manualCommand = computed(() => {
  if (!targetImage.value) return '';
  return [
    `docker pull ${targetImage.value}`,
    '',
    '# 建议先在「系统设置 → 备份恢复」创建一致性备份',
    '# 并记录当前容器配置，便于恢复原启动参数：',
    'docker inspect packbreaker > packbreaker.before-upgrade.json',
    '',
    '# 确认备份与原 docker run 参数都已保存后，再重建容器：',
    'docker rm -f packbreaker',
    '',
    '# 重新执行原 docker run 命令，仅将镜像替换为以下不可变引用：',
    targetImage.value,
  ].join('\n');
});

async function loadCurrentVersion(): Promise<void> {
  try {
    currentVersion.value = (await getSystemHealth()).version;
  } catch {
    // 顶部版本入口不能因为健康接口短暂失败阻断整个应用；保持占位符即可。
  }
}

async function refreshRelease(): Promise<void> {
  releaseLoading.value = true;
  try {
    upgrade.value = await getSystemUpgradeStatus();
    currentVersion.value = upgrade.value.current_version || currentVersion.value;
    releaseChecked.value = true;
  } catch (caught) {
    releaseChecked.value = true;
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    releaseLoading.value = false;
  }
}

async function refreshReleaseQuietly(): Promise<void> {
  try {
    const next = await getSystemUpgradeStatus();
    upgrade.value = next;
    currentVersion.value = next.current_version || currentVersion.value;
    releaseChecked.value = true;
    const phase = next.helper_status?.phase;
    if (phase && !activePhases.has(phase) && phase !== 'idle') {
      upgradeResultUnknown.value = false;
      if (phase !== 'succeeded') {
        targetInFlight.value = '';
        pendingIdempotencyKey.value = '';
      }
    }
    if (
      targetInFlight.value &&
      next.current_version === targetInFlight.value &&
      phase === 'succeeded'
    ) {
      targetInFlight.value = '';
      pendingIdempotencyKey.value = '';
      ElMessage.success(`PackBreaker 已升级到 v${next.current_version}，正在刷新页面。`);
      window.setTimeout(() => window.location.reload(), 600);
    }
  } catch {
    // 容器切换期间主服务会短暂离线；继续轮询，避免把正常重启误报成失败。
  }
}

async function startUpgrade(): Promise<void> {
  const targetVersion = upgrade.value?.latest_version;
  const targetDigest = upgrade.value?.target_image_digest;
  if (!targetVersion || !targetDigest) return;

  if (!upgradeResultUnknown.value) {
    try {
      await ElMessageBox.confirm(
        `升级到 v${targetVersion}？PackBreaker 会先执行安全预检和一致性备份，再启动一个临时 updater 接管容器切换。升级期间页面会短暂断开；新版本健康检查失败时会自动恢复旧容器和数据库。`,
        '确认一键升级',
        {
          confirmButtonText: `立即升级到 v${targetVersion}`,
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
  targetInFlight.value = targetVersion;
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
    if (upgrade.value) upgrade.value.helper_status = result.helper_status;
    ElMessage.success(
      result.idempotency_replayed
        ? '已确认原升级请求，继续等待容器切换完成。'
        : '一次性 updater 已接管升级；页面短暂断开属于正常现象。',
    );
    await refreshReleaseQuietly();
  } catch (caught) {
    const problem = toApiProblem(caught);
    if (
      problem.code === 'API_UNAVAILABLE' ||
      problem.status === 408 ||
      (problem.status ?? 0) >= 500
    ) {
      upgradeResultUnknown.value = true;
      ElMessage.warning('升级请求结果暂时未知；请用同一按钮重试确认，不要重复发起新升级。');
    } else {
      pendingIdempotencyKey.value = '';
      targetInFlight.value = '';
      upgradeResultUnknown.value = false;
      ElMessage.error(problem.message);
    }
  } finally {
    upgradeLoading.value = false;
  }
}

async function copyText(value: string, success: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(value);
    ElMessage.success(success);
  } catch {
    ElMessage.warning('浏览器未允许写入剪贴板，请手动复制。');
  }
}

function open(): void {
  visible.value = true;
}

watch(visible, (isVisible) => {
  if (isVisible && !releaseChecked.value && !releaseLoading.value) void refreshRelease();
});
onMounted(() => {
  void loadCurrentVersion();
  pollTimer = setInterval(() => {
    if (upgradeActive.value || upgradeResultUnknown.value || targetInFlight.value) {
      void refreshReleaseQuietly();
    }
  }, 2500);
});
onUnmounted(() => {
  if (pollTimer !== undefined) clearInterval(pollTimer);
});
defineExpose({ open });
</script>

<template>
  <el-popover
    v-model:visible="visible"
    placement="right-start"
    :width="380"
    :offset="12"
    popper-class="packbreaker-version-popover"
    trigger="click"
  >
    <template #reference>
      <button
        class="brand-version-trigger"
        type="button"
        :class="{ 'has-update': upgrade?.update_available }"
        :aria-label="`当前版本 v${displayedVersion}`"
      >
        v{{ displayedVersion }}
        <span v-if="upgrade?.update_available" class="version-dot" aria-hidden="true"></span>
      </button>
    </template>

    <div class="version-panel">
      <header class="version-panel-header">
        <span>当前版本</span>
        <button
          class="version-icon-button"
          type="button"
          aria-label="检查更新"
          title="检查更新"
          :disabled="releaseLoading"
          @click="refreshRelease"
        >
          <RefreshCw :size="18" :class="{ spinning: releaseLoading }" />
        </button>
      </header>

      <section class="version-current">
        <div class="version-number-row">
          <strong>v{{ displayedVersion }}</strong>
          <span v-if="statusKind === 'latest'" class="version-check"><Check :size="18" /></span>
          <span v-else-if="statusKind === 'update'" class="version-update-mark">NEW</span>
          <span v-else-if="statusKind === 'error'" class="version-error-mark"
            ><TriangleAlert :size="17"
          /></span>
        </div>
        <p :class="`version-status is-${statusKind}`">{{ statusText }}</p>
        <p v-if="statusKind === 'error'" class="version-error-detail">
          {{ upgrade?.release_error_code ?? 'RELEASE_LOOKUP_FAILED' }}
        </p>
        <a class="release-link" :href="releaseUrl" target="_blank" rel="noreferrer">
          <GitBranch :size="19" />查看发布<ExternalLink :size="13" />
        </a>
      </section>

      <div class="version-divider"></div>

      <section v-if="upgrade?.update_available" class="version-auto-upgrade">
        <button
          class="version-upgrade-button"
          type="button"
          :disabled="!canAutoUpgrade"
          @click="startUpgrade"
        >
          <ArrowUpCircle :size="18" />
          {{
            upgradeResultUnknown
              ? '重试确认升级请求'
              : `立即升级到 v${upgrade.latest_version ?? ''}`
          }}
        </button>
        <p v-if="upgradeActive" class="version-upgrade-progress">
          <RefreshCw :size="14" class="spinning" />{{ helperPhaseText }}
        </p>
        <p v-else-if="automaticUpgradeHint" class="version-upgrade-hint">
          {{ automaticUpgradeHint }}
        </p>
        <p v-else class="version-upgrade-hint">自动备份 · 临时 helper 接管 · 健康失败自动回滚</p>
      </section>

      <section class="version-action-section">
        <button class="version-action-row" type="button" @click="manualOpen = !manualOpen">
          <span><Terminal :size="17" />高级：手动 Docker 升级</span>
          <ChevronDown :size="17" :class="{ expanded: manualOpen }" />
        </button>
        <div v-if="manualOpen" class="version-action-detail">
          <template v-if="targetImage">
            <p>
              一键升级不可用时，可在宿主机拉取正式不可变镜像，再沿用原来的
              <code>docker run</code> 参数重建容器。此入口仅作为故障备用。
            </p>
            <div class="version-image-ref">
              <code>{{ targetImage }}</code>
              <button
                type="button"
                aria-label="复制镜像引用"
                @click="copyText(targetImage, '镜像引用已复制')"
              >
                <Copy :size="14" />
              </button>
            </div>
            <pre>{{ manualCommand }}</pre>
            <button
              class="version-copy-button"
              type="button"
              @click="copyText(manualCommand, '手动升级命令已复制')"
            >
              <Copy :size="15" />复制升级命令
            </button>
          </template>
          <template v-else>
            <p>
              尚未取得正式 Release 的不可变镜像。请先重新检查网络，或前往 GitHub Releases
              查看目标版本后再手动升级。
            </p>
          </template>
        </div>
      </section>

      <section class="version-action-section rollback-section">
        <button class="version-action-row" type="button" @click="rollbackOpen = !rollbackOpen">
          <span><RotateCcw :size="17" />版本回退</span>
          <ChevronDown :size="17" :class="{ expanded: rollbackOpen }" />
        </button>
        <div v-if="rollbackOpen" class="version-action-detail">
          <p>
            回退不能只替换旧镜像：如果新版本已经迁移数据库，应恢复与目标版本匹配的升级前一致性备份，
            不执行数据库 downgrade。
          </p>
          <a
            class="rollback-release-link"
            href="https://github.com/YYxiaoma/PackBreaker/releases"
            target="_blank"
            rel="noreferrer"
          >
            <GitBranch :size="15" />选择历史 Release<ExternalLink :size="12" />
          </a>
        </div>
      </section>
    </div>
  </el-popover>
</template>

<style scoped>
.brand-version-trigger {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  width: max-content;
  min-height: 24px;
  padding: 3px 9px;
  border: 0;
  border-radius: 8px;
  color: #66738a;
  background: #f2f5fa;
  font-size: 11px;
  font-weight: 650;
  letter-spacing: 0;
  transition:
    color 0.16s ease,
    background 0.16s ease,
    transform 0.16s ease;
}

.brand-version-trigger:hover,
.brand-version-trigger.has-update {
  color: var(--blue);
  background: var(--blue-soft);
}

.brand-version-trigger:hover {
  transform: translateY(-1px);
}

.version-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--orange);
}

:global(.packbreaker-version-popover.el-popper) {
  max-width: calc(100vw - 24px);
  padding: 0 !important;
  overflow: hidden;
  border: 1px solid var(--line) !important;
  border-radius: 18px !important;
  background: var(--surface) !important;
  box-shadow: 0 20px 55px rgba(28, 43, 67, 0.18) !important;
}

.version-panel {
  color: var(--ink);
}

.version-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 60px;
  padding: 0 20px;
  border-bottom: 1px solid var(--line);
  font-size: 15px;
  font-weight: 650;
}

.version-icon-button {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 0;
  border-radius: 9px;
  color: var(--muted);
  background: transparent;
}

.version-icon-button:hover {
  color: var(--blue);
  background: var(--blue-soft);
}

.version-current {
  display: grid;
  justify-items: center;
  padding: 24px 22px 20px;
  text-align: center;
}

.version-number-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.version-number-row strong {
  color: var(--ink);
  font-size: 34px;
  line-height: 1;
  letter-spacing: -1px;
}

.version-check,
.version-error-mark {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border-radius: 50%;
}

.version-check {
  color: var(--green);
  background: var(--green-soft);
}

.version-error-mark {
  color: var(--orange);
  background: var(--orange-soft);
}

.version-update-mark {
  padding: 4px 7px;
  border-radius: 7px;
  color: #b66a14;
  background: var(--orange-soft);
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.5px;
}

.version-status {
  margin: 12px 0 0;
  color: var(--muted);
  font-size: 13px;
}

.version-status.is-update {
  color: #b66a14;
  font-weight: 650;
}

.version-status.is-error {
  color: var(--orange);
}

.version-error-detail {
  margin: 5px 0 0;
  color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10px;
}

.release-link,
.rollback-release-link {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  margin-top: 18px;
  color: #637089;
  text-decoration: none;
  font-size: 13px;
}

.release-link:hover,
.rollback-release-link:hover {
  color: var(--blue);
}

.version-divider {
  height: 1px;
  margin: 0 20px;
  background: var(--line);
}

.version-auto-upgrade {
  display: grid;
  gap: 9px;
  padding: 16px 20px 12px;
}

.version-upgrade-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 42px;
  padding: 9px 14px;
  border: 0;
  border-radius: 11px;
  color: #fff;
  background: linear-gradient(135deg, #2f6df6, #6c5ce7);
  box-shadow: 0 10px 24px rgba(47, 109, 246, 0.22);
  font-weight: 700;
  transition:
    transform 0.16s ease,
    box-shadow 0.16s ease,
    opacity 0.16s ease;
}

.version-upgrade-button:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: 0 14px 30px rgba(47, 109, 246, 0.28);
}

.version-upgrade-button:disabled {
  box-shadow: none;
  opacity: 0.48;
}

.version-upgrade-hint,
.version-upgrade-progress {
  margin: 0;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.55;
  text-align: center;
}

.version-upgrade-progress {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  color: var(--blue);
  font-weight: 650;
}

.version-action-section {
  padding: 4px 12px;
}

.rollback-section {
  padding-bottom: 10px;
}

.version-action-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-height: 50px;
  padding: 0 8px;
  border: 0;
  color: var(--ink-soft);
  background: transparent;
  text-align: left;
}

.version-action-row > span {
  display: inline-flex;
  align-items: center;
  gap: 9px;
}

.version-action-row > svg {
  color: var(--muted);
  transition: transform 0.16s ease;
}

.version-action-row > svg.expanded {
  transform: rotate(180deg);
}

.version-action-detail {
  padding: 0 8px 14px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.7;
}

.version-action-detail p {
  margin: 0 0 10px;
}

.version-image-ref {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 8px 9px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface-soft);
}

.version-image-ref code {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  color: var(--ink-soft);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.version-image-ref button {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 0;
  border-radius: 7px;
  color: var(--muted);
  background: transparent;
}

.version-action-detail pre {
  max-height: 180px;
  margin: 10px 0;
  padding: 10px;
  overflow: auto;
  border-radius: 9px;
  color: #dce7fb;
  background: #172033;
  font-size: 10px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}

.version-copy-button {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 32px;
  padding: 6px 10px;
  border: 1px solid #cbdafc;
  border-radius: 8px;
  color: var(--blue);
  background: var(--blue-soft);
  font-size: 11px;
  font-weight: 650;
}

.rollback-release-link {
  margin-top: 2px;
  color: var(--blue);
}

.spinning {
  animation: version-spin 0.9s linear infinite;
}

@keyframes version-spin {
  to {
    transform: rotate(360deg);
  }
}

:global(.dark .packbreaker-version-popover.el-popper) {
  border-color: #2e3c50 !important;
  background: #172033 !important;
}

:global(.dark .packbreaker-version-popover .version-action-detail pre) {
  border: 1px solid #36455c;
  background: #101726;
}
</style>
