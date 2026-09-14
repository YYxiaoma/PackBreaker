<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { ElMessage } from 'element-plus';
import {
  Activity,
  Archive,
  Bell,
  CircleCheck,
  Cpu,
  Database,
  Download,
  Globe,
  HardDrive,
  RefreshCw,
  ShieldCheck,
  TriangleAlert,
} from '@lucide/vue';
import { toApiProblem } from '../api/client';
import {
  exportSystemDiagnostics,
  getSystemHealth,
  type SystemHealth,
  type SystemHealthCheck,
} from '../api/system';

const health = ref<SystemHealth | null>(null);
const loading = ref(false);
const exportLoading = ref(false);
let refreshTimer: ReturnType<typeof setInterval> | undefined;

type HealthTagType = 'success' | 'warning' | 'danger' | 'info';

const checkLabels: Record<string, string> = {
  runtime: '运行时安全门',
  storage: '磁盘空间',
  backups: '一致性备份',
  tasks: '任务积压',
  operations: '操作对账',
  sites: 'PT 站点',
  downloaders: '下载器',
  notifications: '通知投递',
  workers: '后台 Driver',
};

const metricLabels: Record<string, string> = {
  total: '总数',
  active: '活动',
  retry: '重试',
  stale_active: '陈旧活动任务',
  awaiting_confirmation: '待人工确认',
  reconcile_required: '待只读对账',
  rollback_blocked: '回滚阻断',
  stale_unsettled: '长时间未收敛',
  configured: '已配置',
  enabled: '已启用',
  connection_failed: '连接失败',
  connection_untested: '未测试连接',
  circuit_open: '熔断 OPEN',
  circuit_half_open: '熔断 HALF_OPEN',
  path_mapping_failed: '路径诊断失败',
  path_mapping_untested: '路径未诊断',
  dead_deliveries: 'DEAD 投递',
  enabled_not_ok: '启用但非 OK',
  valid_backup_count: '有效备份',
  blocked_backup_count: '异常备份对',
  latest_backup_age_hours: '最近备份年龄',
  config_free_bytes: '配置卷可用',
  data_free_bytes: '数据卷可用',
  config_free_ratio: '配置卷剩余比例',
  data_free_ratio: '数据卷剩余比例',
  task_consecutive_errors: '任务 Driver 连续错误',
  history_consecutive_errors: '历史 Driver 连续错误',
  notification_consecutive_errors: '通知 Driver 连续错误',
};

function check(name: string): SystemHealthCheck | undefined {
  return health.value?.checks.find((item) => item.name === name);
}

function metricNumber(checkName: string, key: string): number {
  const value = check(checkName)?.metrics[key];
  return typeof value === 'number' ? value : 0;
}

const activeTasks = computed(() => metricNumber('tasks', 'active'));
const operationAttention = computed(
  () =>
    metricNumber('operations', 'reconcile_required') +
    metricNumber('operations', 'rollback_blocked') +
    metricNumber('operations', 'stale_unsettled'),
);
const backupAge = computed(() => {
  const value = check('backups')?.metrics.latest_backup_age_hours;
  return typeof value === 'number' ? `${value.toFixed(value < 10 ? 1 : 0)}h` : '—';
});

function statusLabel(status: SystemHealth['status']): string {
  return { ok: '正常', warning: '需要关注', blocked: '阻断' }[status];
}

function tagType(status: SystemHealth['status']): HealthTagType {
  return status === 'ok' ? 'success' : status === 'warning' ? 'warning' : 'danger';
}

function formatBytes(value: number): string {
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} GiB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${Math.round(value / 1024)} KiB`;
}

function formatMetric(key: string, value: string | number | boolean | null): string {
  if (value === null) return '—';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'number' && key.endsWith('_bytes')) return formatBytes(value);
  if (typeof value === 'number' && key.endsWith('_ratio')) return `${(value * 100).toFixed(1)}%`;
  if (typeof value === 'number' && key.endsWith('_age_hours')) return `${value.toFixed(1)} h`;
  return String(value);
}

async function refresh(silent = false): Promise<void> {
  if (!silent) loading.value = true;
  try {
    health.value = await getSystemHealth();
  } catch (caught) {
    const problem = toApiProblem(caught);
    if (!silent) ElMessage.error(problem.message);
  } finally {
    if (!silent) loading.value = false;
  }
}

function downloadArtifact(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

async function downloadDiagnostics(): Promise<void> {
  exportLoading.value = true;
  try {
    const artifact = await exportSystemDiagnostics();
    downloadArtifact(artifact.blob, artifact.filename);
    ElMessage.success('安全诊断包已生成；不包含日志、URL、路径、任务 ID、hash 或凭证');
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    exportLoading.value = false;
  }
}

onMounted(() => {
  void refresh();
  refreshTimer = setInterval(() => void refresh(true), 30_000);
});

onUnmounted(() => {
  if (refreshTimer !== undefined) clearInterval(refreshTimer);
});
</script>

<template>
  <div class="ops-overview" v-loading="loading">
    <div class="ops-actions">
      <div>
        <h2>运行态势 <small>只汇总已有证据，不主动探测外部服务</small></h2>
        <p v-if="health">
          数据生成于 {{ new Date(health.generated_at).toLocaleString() }} · v{{ health.version }}
        </p>
      </div>
      <div>
        <el-button @click="refresh()"><RefreshCw :size="15" />刷新</el-button>
        <el-button :loading="exportLoading" @click="downloadDiagnostics">
          <Download :size="15" />安全诊断包
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="health"
      :title="`系统运维状态：${statusLabel(health.status)}`"
      :type="health.status === 'ok' ? 'success' : health.status === 'warning' ? 'warning' : 'error'"
      :description="
        health.status === 'ok'
          ? '本地运行时、依赖既有证据和后台 Driver 未发现需要处理的异常。'
          : '下方卡片会标出 warning / blocked；外部依赖 warning 不会错误影响容器 readiness。'
      "
      :closable="false"
      show-icon
    />

    <div v-if="health" class="ops-summary">
      <article>
        <ShieldCheck :size="20" />
        <span>整体状态</span>
        <strong>{{ statusLabel(health.status) }}</strong>
        <el-tag :type="tagType(health.status)">{{ health.status.toUpperCase() }}</el-tag>
      </article>
      <article>
        <Activity :size="20" />
        <span>活动任务</span>
        <strong>{{ activeTasks }}</strong>
        <small>非终态任务</small>
      </article>
      <article>
        <TriangleAlert :size="20" />
        <span>Operation 关注项</span>
        <strong>{{ operationAttention }}</strong>
        <small>对账 / 回滚阻断 / 陈旧</small>
      </article>
      <article>
        <Archive :size="20" />
        <span>最近有效备份</span>
        <strong>{{ backupAge }}</strong>
        <small>{{ check('backups')?.code ?? 'BACKUP_UNKNOWN' }}</small>
      </article>
    </div>

    <div v-if="health" class="health-grid">
      <article v-for="item in health.checks" :key="item.name" class="health-card">
        <header>
          <div class="health-icon">
            <Database v-if="item.name === 'runtime'" :size="19" />
            <HardDrive
              v-else-if="item.name === 'storage' || item.name === 'downloaders'"
              :size="19"
            />
            <Archive v-else-if="item.name === 'backups'" :size="19" />
            <Activity v-else-if="item.name === 'tasks'" :size="19" />
            <ShieldCheck v-else-if="item.name === 'operations'" :size="19" />
            <Globe v-else-if="item.name === 'sites'" :size="19" />
            <Bell v-else-if="item.name === 'notifications'" :size="19" />
            <Cpu v-else :size="19" />
          </div>
          <div>
            <b>{{ checkLabels[item.name] ?? item.name }}</b>
            <small>{{ item.code }}</small>
          </div>
          <el-tag :type="tagType(item.status)">{{ statusLabel(item.status) }}</el-tag>
        </header>
        <p>{{ item.detail }}</p>
        <dl v-if="Object.keys(item.metrics).length">
          <template v-for="(value, key) in item.metrics" :key="key">
            <dt>{{ metricLabels[key] ?? key }}</dt>
            <dd>{{ formatMetric(key, value) }}</dd>
          </template>
        </dl>
        <div v-else class="health-empty"><CircleCheck :size="15" />当前检查没有额外计数</div>
      </article>
    </div>

    <el-empty v-else-if="!loading" description="健康状态暂不可用" />
  </div>
</template>

<style scoped>
.ops-overview {
  display: grid;
  gap: 18px;
}
.ops-actions {
  display: flex;
  justify-content: space-between;
  gap: 18px;
  align-items: center;
}
.ops-actions h2,
.ops-actions p {
  margin: 0;
}
.ops-actions h2 small {
  margin-left: 10px;
  color: var(--muted);
  font-size: 11px;
  font-weight: 400;
}
.ops-actions p {
  color: var(--muted);
  font-size: 11px;
  margin-top: 7px;
}
.ops-actions > div:last-child {
  display: flex;
  gap: 8px;
}
.ops-summary {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border: 1px solid var(--line);
  border-radius: 7px;
  background: var(--surface);
}
.ops-summary article {
  padding: 20px;
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 7px 10px;
  border-right: 1px solid var(--line);
}
.ops-summary article:last-child {
  border-right: 0;
}
.ops-summary svg {
  color: var(--blue);
}
.ops-summary span,
.ops-summary small {
  color: var(--muted);
  font-size: 11px;
}
.ops-summary strong {
  grid-column: 1 / -1;
  font-size: 27px;
  font-weight: 650;
}
.ops-summary .el-tag {
  width: max-content;
}
.health-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
}
.health-card {
  border: 1px solid var(--line);
  border-radius: 7px;
  background: var(--surface);
  padding: 18px;
  min-width: 0;
}
.health-card header {
  display: flex;
  gap: 10px;
  align-items: center;
}
.health-card header > div:nth-child(2) {
  min-width: 0;
  flex: 1;
}
.health-card header b,
.health-card header small {
  display: block;
}
.health-card header small {
  color: var(--muted);
  font-size: 10px;
  margin-top: 3px;
  overflow-wrap: anywhere;
}
.health-icon {
  width: 34px;
  height: 34px;
  border-radius: 6px;
  background: var(--blue-soft);
  color: var(--blue);
  display: grid;
  place-items: center;
  flex: 0 0 auto;
}
.health-card > p {
  min-height: 38px;
  margin: 14px 0;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.7;
}
dl {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 7px 12px;
  margin: 0;
  padding-top: 12px;
  border-top: 1px solid var(--line);
  font-size: 11px;
}
dt {
  color: var(--muted);
  min-width: 0;
  overflow-wrap: anywhere;
}
dd {
  margin: 0;
  font-family: Consolas, monospace;
  text-align: right;
}
.health-empty {
  border-top: 1px solid var(--line);
  padding-top: 12px;
  color: var(--muted);
  font-size: 11px;
  display: flex;
  gap: 6px;
  align-items: center;
}
@media (max-width: 1200px) {
  .health-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .ops-summary {
    grid-template-columns: repeat(2, 1fr);
  }
  .ops-summary article:nth-child(2) {
    border-right: 0;
  }
  .ops-summary article:nth-child(-n + 2) {
    border-bottom: 1px solid var(--line);
  }
}
@media (max-width: 800px) {
  .ops-actions {
    align-items: flex-start;
    flex-direction: column;
  }
  .ops-actions > div:last-child {
    width: 100%;
  }
  .ops-actions .el-button {
    flex: 1;
  }
  .health-grid,
  .ops-summary {
    grid-template-columns: 1fr;
  }
  .ops-summary article,
  .ops-summary article:nth-child(2) {
    border-right: 0;
    border-bottom: 1px solid var(--line);
  }
  .ops-summary article:last-child {
    border-bottom: 0;
  }
}
</style>
