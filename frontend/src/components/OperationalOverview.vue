<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { ElMessage } from 'element-plus';
import {
  Activity,
  ArrowRight,
  Archive,
  Bell,
  CircleCheck,
  Cpu,
  Database,
  GitBranch,
  Globe,
  HardDrive,
  ListChecks,
  RefreshCw,
  Search,
  ScrollText,
  Settings2,
  ShieldCheck,
  TriangleAlert,
} from '@lucide/vue';
import { toApiProblem } from '../api/client';
import { listTasks, type TaskRecord, type TaskStatus } from '../api/tasks';
import {
  getSystemHealth,
  getSystemUpgradeStatus,
  type SystemHealth,
  type SystemHealthCheck,
  type SystemUpgradeStatus,
} from '../api/system';

const emit = defineEmits<{
  navigate: [page: string];
  openVersion: [];
}>();
const health = ref<SystemHealth | null>(null);
const tasks = ref<TaskRecord[]>([]);
const upgrade = ref<SystemUpgradeStatus | null>(null);
const loading = ref(false);
let refreshTimer: ReturnType<typeof setInterval> | undefined;
let upgradeRefreshTimer: ReturnType<typeof setInterval> | undefined;

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
const completedTasks = computed(() => tasks.value.filter((task) => task.status === 'DONE').length);
const failedTasks = computed(() => tasks.value.filter((task) => task.status === 'FAILED').length);
const awaitingConfirmation = computed(
  () => tasks.value.filter((task) => task.status === 'AWAITING_CONFIRMATION').length,
);
const operationAttention = computed(
  () =>
    metricNumber('operations', 'reconcile_required') +
    metricNumber('operations', 'rollback_blocked') +
    metricNumber('operations', 'stale_unsettled'),
);
const healthScore = computed(() => {
  if (!health.value?.checks.length) return 0;
  const ok = health.value.checks.filter((item) => item.status === 'ok').length;
  return Math.round((ok / health.value.checks.length) * 1000) / 10;
});
const riskCount = computed(() => {
  const unhealthyChecks = health.value?.checks.filter((item) => item.status !== 'ok').length ?? 0;
  return unhealthyChecks + failedTasks.value + operationAttention.value;
});
const backupAge = computed(() => {
  const value = check('backups')?.metrics.latest_backup_age_hours;
  return typeof value === 'number' ? `${value.toFixed(value < 10 ? 1 : 0)}h` : '—';
});

function countStatuses(statuses: TaskStatus[]): number {
  return tasks.value.filter((task) => statuses.includes(task.status)).length;
}

const pipeline = computed(() => [
  {
    key: 'analysis',
    label: '待分析',
    note: '等待进入分析',
    count: countStatuses(['PENDING', 'ANALYZING']),
    icon: Database,
    tone: 'blue',
  },
  {
    key: 'search',
    label: '搜索验证',
    note: '候选发现与验证',
    count: countStatuses(['SEARCHING', 'MATCHING', 'VERIFYING']),
    icon: Search,
    tone: 'cyan',
  },
  {
    key: 'preflight',
    label: '安全预演',
    note: '生成执行前证据',
    count: countStatuses(['PREFLIGHT']),
    icon: ShieldCheck,
    tone: 'purple',
  },
  {
    key: 'review',
    label: '待确认',
    note: '需要人工确认',
    count: countStatuses(['AWAITING_CONFIRMATION', 'PAUSED', 'RETRY']),
    icon: Bell,
    tone: 'orange',
  },
  {
    key: 'execute',
    label: '执行中',
    note: '链接 / 添加 / 校验 / 做种',
    count: countStatuses([
      'LINKING',
      'ADDING',
      'CLIENT_VERIFYING',
      'SEEDING',
      'CANCELLING',
      'ROLLING_BACK',
    ]),
    icon: Activity,
    tone: 'green',
  },
  {
    key: 'completed',
    label: '已完成',
    note: '任务已安全收敛',
    count: countStatuses(['DONE']),
    icon: CircleCheck,
    tone: 'emerald',
  },
]);

const recentTasks = computed(() =>
  [...tasks.value]
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at))
    .slice(0, 5),
);

function versionStatusLabel(): string {
  if (!upgrade.value) return '检查中';
  if (upgrade.value.release_error_code) return '检查失败';
  return upgrade.value.update_available ? '有更新' : '最新';
}

function versionStatusClass(): string {
  if (!upgrade.value) return 'is-warning';
  if (upgrade.value.release_error_code || upgrade.value.update_available) return 'is-warning';
  return 'is-ok';
}

function taskStatusLabel(status: TaskStatus): string {
  return (
    {
      PENDING: '等待分析',
      ANALYZING: '分析中',
      SEARCHING: '搜索中',
      MATCHING: '匹配中',
      VERIFYING: '验证中',
      PREFLIGHT: '预演中',
      AWAITING_CONFIRMATION: '待确认',
      LINKING: '创建链接',
      ADDING: '添加下载器',
      CLIENT_VERIFYING: '客户端校验',
      SEEDING: '做种中',
      DONE: '已完成',
      PAUSED: '已暂停',
      RETRY: '待重试',
      FAILED: '失败',
      CANCELLING: '取消中',
      ROLLING_BACK: '回滚中',
      CANCELLED: '已取消',
    }[status] ?? status
  );
}

function taskTone(status: TaskStatus): HealthTagType {
  if (status === 'DONE' || status === 'SEEDING') return 'success';
  if (status === 'FAILED' || status === 'CANCELLED') return 'danger';
  if (['AWAITING_CONFIRMATION', 'PAUSED', 'RETRY'].includes(status)) return 'warning';
  return 'info';
}

function go(page: string): void {
  emit('navigate', page);
}

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

async function refreshUpgrade(): Promise<void> {
  try {
    upgrade.value = await getSystemUpgradeStatus();
  } catch {
    // 升级发现依赖外部 Release；总览首屏与核心健康信息不应被它阻塞。
  }
}

async function refresh(silent = false): Promise<void> {
  const showInitialLoading = !silent && health.value === null;
  if (showInitialLoading) loading.value = true;
  const taskRequest = listTasks().catch(() => tasks.value);
  try {
    health.value = await getSystemHealth();
    if (showInitialLoading) loading.value = false;
    tasks.value = await taskRequest;
  } catch (caught) {
    const problem = toApiProblem(caught);
    if (!silent) ElMessage.error(problem.message);
  } finally {
    if (showInitialLoading) loading.value = false;
  }
}

onMounted(() => {
  void refresh().then(() => window.setTimeout(() => void refreshUpgrade(), 160));
  refreshTimer = setInterval(() => void refresh(true), 30_000);
  upgradeRefreshTimer = setInterval(() => void refreshUpgrade(), 300_000);
});

onUnmounted(() => {
  if (refreshTimer !== undefined) clearInterval(refreshTimer);
  if (upgradeRefreshTimer !== undefined) clearInterval(upgradeRefreshTimer);
});
</script>

<template>
  <div class="ops-overview" v-loading="loading">
    <template v-if="health">
      <div class="overview-page-heading">
        <div>
          <h1>总览</h1>
          <span><Activity :size="14" />实时运行态势</span>
        </div>
        <small>
          {{ new Date(health.generated_at).toLocaleString() }} · v{{ health.version }} ·
          {{ statusLabel(health.status) }}
        </small>
      </div>

      <section class="overview-kpis">
        <article class="overview-kpi kpi-green">
          <span class="kpi-icon"><Activity :size="22" /></span>
          <div>
            <small>运行健康度</small><strong>{{ healthScore }}<em>%</em></strong>
          </div>
          <el-tag :type="tagType(health.status)" size="small">{{
            statusLabel(health.status)
          }}</el-tag>
          <span class="kpi-caption"
            >{{ health.checks.filter((item) => item.status === 'ok').length }} /
            {{ health.checks.length }} 项检查正常</span
          >
          <i class="kpi-progress"><b :style="{ width: `${healthScore}%` }"></b></i>
        </article>
        <article class="overview-kpi kpi-blue">
          <span class="kpi-icon"><ListChecks :size="22" /></span>
          <div>
            <small>进行中任务</small><strong>{{ activeTasks }}</strong>
          </div>
          <span class="kpi-badge">共 {{ tasks.length }} 个任务</span>
          <span class="kpi-caption">实时统计所有非终态任务</span>
          <i class="kpi-progress"
            ><b
              :style="{
                width: `${Math.min(100, tasks.length ? (activeTasks / tasks.length) * 100 : 0)}%`,
              }"
            ></b
          ></i>
        </article>
        <article class="overview-kpi kpi-orange">
          <span class="kpi-icon"><Bell :size="22" /></span>
          <div>
            <small>待审核</small><strong>{{ awaitingConfirmation }}</strong>
          </div>
          <span class="kpi-badge">人工确认</span>
          <span class="kpi-caption">等待人工确认的任务</span>
          <i class="kpi-progress"
            ><b :style="{ width: `${Math.min(100, awaitingConfirmation * 14)}%` }"></b
          ></i>
        </article>
        <article class="overview-kpi kpi-cyan">
          <span class="kpi-icon"><CircleCheck :size="22" /></span>
          <div>
            <small>已完成</small><strong>{{ completedTasks }}</strong>
          </div>
          <span class="kpi-badge">已完成</span>
          <span class="kpi-caption">已安全收敛的任务</span>
          <i class="kpi-progress"
            ><b
              :style="{
                width: `${Math.min(100, tasks.length ? (completedTasks / tasks.length) * 100 : 0)}%`,
              }"
            ></b
          ></i>
        </article>
        <article class="overview-kpi kpi-red">
          <span class="kpi-icon"><TriangleAlert :size="22" /></span>
          <div>
            <small>风险告警</small><strong>{{ riskCount }}</strong>
          </div>
          <span class="kpi-badge">需关注</span>
          <span class="kpi-caption">健康异常、失败任务与对账关注项</span>
          <i class="kpi-progress"
            ><b :style="{ width: `${Math.min(100, riskCount * 12)}%` }"></b
          ></i>
        </article>
      </section>

      <section class="overview-primary-grid">
        <article class="overview-card pipeline-card">
          <header class="overview-card-head">
            <div>
              <span class="section-icon"><GitBranch :size="18" /></span>
              <h3>任务流水线状态</h3>
            </div>
          </header>
          <div class="pipeline-orbit" aria-label="任务流水线阶段分布">
            <div class="pipeline-orbit-ring" aria-hidden="true"></div>
            <svg
              class="pipeline-flow-arrows"
              viewBox="0 0 1000 620"
              preserveAspectRatio="none"
              aria-hidden="true"
            >
              <defs>
                <linearGradient id="pipeline-flow-gradient" x1="0" y1="0" x2="1" y2="1">
                  <stop offset="0%" stop-color="#73a4ff" />
                  <stop offset="52%" stop-color="#7b84f7" />
                  <stop offset="100%" stop-color="#42c7ad" />
                </linearGradient>
                <marker
                  id="pipeline-arrowhead"
                  markerWidth="8"
                  markerHeight="8"
                  refX="6.5"
                  refY="4"
                  orient="auto"
                  markerUnits="strokeWidth"
                >
                  <path d="M 0 0 L 8 4 L 0 8 z" />
                </marker>
              </defs>
              <path d="M 570 90 C 690 84, 790 126, 842 210" />
              <path d="M 865 236 C 914 302, 914 372, 866 438" />
              <path d="M 830 468 C 748 548, 650 570, 575 558" />
              <path d="M 425 558 C 328 570, 236 536, 170 468" />
              <path d="M 136 438 C 88 372, 88 302, 136 236" />
              <path d="M 160 210 C 214 126, 310 84, 430 90" />
            </svg>
            <div class="pipeline-hub">
              <span class="pipeline-hub-icon"><GitBranch :size="25" /></span>
              <b>任务流水线</b>
              <small>总任务</small>
              <strong>{{ tasks.length }}</strong>
              <span>{{ activeTasks }} 个正在流转</span>
            </div>
            <div
              v-for="stage in pipeline"
              :key="stage.key"
              :class="['pipeline-orbit-stage', `orbit-${stage.key}`, `stage-${stage.tone}`]"
            >
              <span class="pipeline-icon"><component :is="stage.icon" :size="20" /></span>
              <div>
                <b>{{ stage.label }}</b>
                <small>{{ stage.note }}</small>
              </div>
              <strong>{{ stage.count }}</strong>
            </div>
          </div>
        </article>

        <article class="overview-card services-card">
          <header class="overview-card-head service-card-head">
            <div class="service-heading">
              <span class="section-icon"><Cpu :size="20" /></span>
              <div class="service-heading-copy">
                <h3>系统服务状态</h3>
                <p>核心服务运行状态总览</p>
              </div>
            </div>
            <span class="all-good" :class="{ warning: health.status !== 'ok' }">
              <i></i>{{ health.status === 'ok' ? '服务运行正常' : '服务需要关注' }}
            </span>
          </header>
          <div class="service-grid">
            <button class="service-tile service-downloaders" @click="go('下载器')">
              <div class="service-main">
                <span class="service-icon blue"><HardDrive :size="24" /></span>
                <div class="service-copy">
                  <div class="service-title-row">
                    <b>下载器</b>
                    <small :class="`is-${check('downloaders')?.status ?? 'warning'}`">{{
                      statusLabel(check('downloaders')?.status ?? 'warning')
                    }}</small>
                  </div>
                  <strong
                    >{{ metricNumber('downloaders', 'enabled') }} /
                    {{ metricNumber('downloaders', 'configured') }}</strong
                  >
                  <span class="service-value-note">已启用 / 已配置</span>
                  <p>负责样本下载与任务管理</p>
                </div>
              </div>
              <span class="service-visual blue" aria-hidden="true"><HardDrive :size="98" /></span>
            </button>
            <button class="service-tile service-sites" @click="go('站点管理')">
              <div class="service-main">
                <span class="service-icon cyan"><Globe :size="24" /></span>
                <div class="service-copy">
                  <div class="service-title-row">
                    <b>站点</b>
                    <small :class="`is-${check('sites')?.status ?? 'warning'}`">{{
                      statusLabel(check('sites')?.status ?? 'warning')
                    }}</small>
                  </div>
                  <strong
                    >{{ metricNumber('sites', 'enabled') }} /
                    {{ metricNumber('sites', 'configured') }}</strong
                  >
                  <span class="service-value-note">已启用 / 已配置</span>
                  <p>管理分析站点与连接状态</p>
                </div>
              </div>
              <span class="service-visual cyan" aria-hidden="true"><Globe :size="104" /></span>
            </button>
            <button class="service-tile service-backups" @click="go('系统设置')">
              <div class="service-main">
                <span class="service-icon green"><Archive :size="24" /></span>
                <div class="service-copy">
                  <div class="service-title-row">
                    <b>备份</b>
                    <small :class="`is-${check('backups')?.status ?? 'warning'}`">{{
                      statusLabel(check('backups')?.status ?? 'warning')
                    }}</small>
                  </div>
                  <strong>{{ metricNumber('backups', 'valid_backup_count') }}</strong>
                  <span class="service-value-note">最近有效备份 {{ backupAge }}</span>
                  <p>备份数据与恢复保障</p>
                </div>
              </div>
              <span class="service-visual green" aria-hidden="true"><Archive :size="98" /></span>
            </button>
            <button class="service-tile service-updater" @click="emit('openVersion')">
              <div class="service-main">
                <span class="service-icon purple"><RefreshCw :size="24" /></span>
                <div class="service-copy">
                  <div class="service-title-row">
                    <b>版本</b
                    ><small :class="versionStatusClass()">{{ versionStatusLabel() }}</small>
                  </div>
                  <strong>v{{ upgrade?.current_version ?? health.version }}</strong>
                  <span class="service-value-note">{{
                    upgrade?.release_error_code
                      ? '暂时无法检查正式版本'
                      : upgrade?.update_available
                        ? `可升级到 v${upgrade.latest_version}`
                        : '当前已是最新正式版本'
                  }}</span>
                  <p>正式版本发现与一键升级</p>
                </div>
              </div>
              <span class="service-visual purple" aria-hidden="true"
                ><RefreshCw :size="102"
              /></span>
            </button>
          </div>
        </article>
      </section>

      <section class="overview-secondary-grid">
        <article class="overview-card activity-card">
          <header class="overview-card-head">
            <div>
              <span class="section-icon"><Activity :size="18" /></span>
              <h3>最近动态</h3>
            </div>
            <button @click="go('任务中心')">查看全部 <ArrowRight :size="14" /></button>
          </header>
          <div v-if="recentTasks.length" class="activity-list">
            <button v-for="task in recentTasks" :key="task.id" @click="go('任务中心')">
              <time>{{
                new Date(task.updated_at).toLocaleTimeString([], {
                  hour: '2-digit',
                  minute: '2-digit',
                })
              }}</time>
              <span class="activity-dot" :class="`tone-${taskTone(task.status)}`"></span>
              <div>
                <b>{{ task.type }} · Run #{{ task.run_number }}</b
                ><small>{{ task.normalized_unit_key }}</small>
              </div>
              <el-tag :type="taskTone(task.status)" size="small">{{
                taskStatusLabel(task.status)
              }}</el-tag>
            </button>
          </div>
          <el-empty v-else description="当前没有任务动态" :image-size="64" />
        </article>

        <article class="overview-card quick-card">
          <header class="overview-card-head">
            <div>
              <span class="section-icon"><ListChecks :size="18" /></span>
              <h3>快捷操作</h3>
            </div>
          </header>
          <div class="quick-grid">
            <button @click="go('任务中心')">
              <span class="quick-icon blue"><ListChecks :size="20" /></span><b>任务中心</b
              ><small>登记、分析与跟踪任务</small>
            </button>
            <button @click="go('预演与确认')">
              <span class="quick-icon orange"><GitBranch :size="20" /></span><b>审核中心</b
              ><small>检查证据与人工确认</small>
            </button>
            <button @click="go('站点管理')">
              <span class="quick-icon cyan"><Globe :size="20" /></span><b>站点管理</b
              ><small>连接、Cookie 与可靠性</small>
            </button>
            <button @click="go('下载器')">
              <span class="quick-icon purple"><HardDrive :size="20" /></span><b>下载器管理</b
              ><small>连接与路径映射</small>
            </button>
            <button @click="go('日志')">
              <span class="quick-icon blue"><ScrollText :size="20" /></span><b>运行日志</b
              ><small>检索与导出脱敏日志</small>
            </button>
            <button @click="go('系统设置')">
              <span class="quick-icon green"><Settings2 :size="20" /></span><b>系统设置</b
              ><small>通知与备份恢复</small>
            </button>
          </div>
        </article>

        <aside class="summary-stack">
          <button class="summary-mini" @click="go('日志')">
            <span class="summary-icon purple"><ScrollText :size="22" /></span>
            <div>
              <small>运行日志</small><strong>可检索</strong
              ><span>最近健康快照 {{ new Date(health.generated_at).toLocaleTimeString() }}</span>
            </div>
            <ArrowRight :size="16" />
          </button>
          <button class="summary-mini" @click="go('清理与对账')">
            <span class="summary-icon orange"><ShieldCheck :size="22" /></span>
            <div>
              <small>清理与对账</small><strong>{{ operationAttention }}</strong
              ><span>当前需要关注的 Operation 记录</span>
            </div>
            <ArrowRight :size="16" />
          </button>
        </aside>
      </section>

      <details class="health-details">
        <summary><ShieldCheck :size="16" />查看完整健康检查与指标</summary>
        <div class="health-grid health-grid-compact">
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
                <b>{{ checkLabels[item.name] ?? item.name }}</b
                ><small>{{ item.code }}</small>
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
      </details>
    </template>

    <el-empty v-else-if="!loading" description="健康状态暂不可用" />
  </div>
</template>

<style scoped>
.ops-overview {
  display: grid;
  gap: 18px;
}
.ops-overview {
  width: 100%;
  min-width: 0;
}
.overview-page-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  min-width: 0;
}
.overview-page-heading > div {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.overview-page-heading h1 {
  margin: 0;
  color: #1b2947;
  font-size: 22px;
  line-height: 1.2;
}
.overview-page-heading span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--blue);
  font-size: 11px;
  font-weight: 700;
}
.overview-page-heading small {
  color: #8b97aa;
  font-size: 10px;
  white-space: nowrap;
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
  grid-template-columns: repeat(auto-fit, minmax(135px, 1fr));
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

/* 运营总览指挥台 */
.ops-overview {
  gap: 14px;
}
.overview-hero {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(420px, 0.85fr);
  min-height: 176px;
  overflow: hidden;
  border: 1px solid #dfe8f6;
  border-radius: 22px;
  background: linear-gradient(
    105deg,
    rgba(255, 255, 255, 0.98) 0%,
    rgba(247, 251, 255, 0.96) 53%,
    rgba(235, 244, 255, 0.94) 100%
  );
  box-shadow: 0 14px 38px rgba(50, 80, 125, 0.08);
}
.overview-hero-copy {
  position: relative;
  z-index: 4;
  padding: 28px 30px;
}
.overview-kicker {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--blue);
  font-size: 11px;
  font-weight: 760;
  letter-spacing: 0.6px;
}
.overview-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.overview-title-row h1 {
  margin: 0;
  color: #31405f;
  font-size: 12px;
  font-weight: 760;
  letter-spacing: 0.4px;
}
.overview-title-row h1::after {
  content: '';
  display: inline-block;
  width: 1px;
  height: 12px;
  margin-left: 10px;
  vertical-align: -2px;
  background: #d8e1ef;
}
.overview-hero h2 {
  margin: 8px 0 7px;
  color: #14224a;
  font-size: clamp(29px, 2.1vw, 39px);
  line-height: 1.12;
  letter-spacing: -1.2px;
}
.overview-hero-copy > p {
  max-width: 720px;
  margin: 0;
  color: #7787a4;
  font-size: 12px;
  line-height: 1.8;
}
.overview-hero-actions {
  display: flex;
  gap: 8px;
  margin-top: 19px;
  flex-wrap: wrap;
}
.overview-hero-visual {
  position: relative;
  min-height: 176px;
  overflow: hidden;
  background:
    radial-gradient(circle at 72% 30%, rgba(108, 162, 255, 0.28), transparent 24%),
    linear-gradient(155deg, rgba(228, 241, 255, 0.1), rgba(210, 231, 255, 0.76));
}
.overview-hero-visual::before {
  content: '让每一份资源都发挥更大的价值';
  position: absolute;
  z-index: 4;
  left: 6%;
  top: 24px;
  max-width: 220px;
  color: #376ecb;
  font-size: 14px;
  font-style: italic;
  line-height: 1.55;
  transform: rotate(-4deg);
  opacity: 0.84;
}
.hero-mountain {
  position: absolute;
  bottom: -12px;
  width: 100%;
  height: 112px;
  transform-origin: bottom;
  clip-path: polygon(
    0 100%,
    7% 83%,
    16% 88%,
    27% 54%,
    35% 74%,
    47% 22%,
    57% 62%,
    66% 39%,
    77% 69%,
    86% 45%,
    100% 80%,
    100% 100%
  );
}
.mountain-back {
  left: -3%;
  width: 108%;
  bottom: 24px;
  background: linear-gradient(180deg, rgba(114, 161, 233, 0.2), rgba(161, 197, 244, 0.04));
  transform: scaleY(1.1);
}
.mountain-mid {
  left: 2%;
  width: 102%;
  background: linear-gradient(180deg, rgba(100, 149, 225, 0.42), rgba(199, 220, 249, 0.22));
}
.mountain-front {
  left: 10%;
  width: 96%;
  bottom: -23px;
  background: linear-gradient(180deg, rgba(72, 124, 210, 0.32), rgba(217, 232, 251, 0.84));
  transform: scaleY(0.92);
}
.hero-time {
  position: absolute;
  z-index: 5;
  top: 22px;
  right: 22px;
  display: grid;
  justify-items: end;
  padding: 12px 14px;
  border: 1px solid rgba(214, 225, 242, 0.9);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.78);
  box-shadow: 0 10px 30px rgba(52, 87, 132, 0.08);
  backdrop-filter: blur(12px);
}
.hero-time small {
  color: #8a97ab;
  font-size: 10px;
}
.hero-time strong {
  margin: 3px 0 5px;
  color: #15213e;
  font-size: 28px;
  line-height: 1;
}
.hero-time span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #617089;
  font-size: 10px;
}
.hero-time i,
.all-good i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 0 4px rgba(32, 168, 120, 0.1);
}
.overview-kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 10px;
}
.overview-kpi {
  position: relative;
  min-width: 0;
  min-height: 145px;
  padding: 16px;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 17px;
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}
.overview-kpi::after {
  content: '';
  position: absolute;
  width: 105px;
  height: 105px;
  right: -44px;
  top: -49px;
  border-radius: 50%;
  background: currentColor;
  opacity: 0.045;
}
.overview-kpi > div {
  display: grid;
  gap: 3px;
  margin-left: 48px;
  min-height: 48px;
}
.kpi-icon {
  position: absolute;
  top: 16px;
  left: 16px;
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border-radius: 11px;
  color: currentColor;
  background: color-mix(in srgb, currentColor 11%, white);
}
.overview-kpi small {
  color: #66748a;
  font-size: 11px;
  font-weight: 700;
}
.overview-kpi strong {
  color: #17213e;
  font-size: 28px;
  line-height: 1;
}
.overview-kpi strong em {
  margin-left: 2px;
  font-size: 13px;
  font-style: normal;
  font-weight: 700;
}
.overview-kpi > .el-tag,
.kpi-badge {
  position: absolute;
  top: 16px;
  right: 13px;
}
.kpi-badge {
  padding: 4px 7px;
  border-radius: 7px;
  color: currentColor;
  background: color-mix(in srgb, currentColor 8%, white);
  font-size: 9px;
  font-weight: 720;
}
.kpi-caption {
  display: block;
  margin-top: 14px;
  color: #8b97aa;
  font-size: 9.5px;
  line-height: 1.35;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.kpi-progress {
  position: absolute;
  left: 16px;
  right: 16px;
  bottom: 14px;
  height: 5px;
  overflow: hidden;
  border-radius: 999px;
  background: color-mix(in srgb, currentColor 8%, white);
}
.kpi-progress b {
  display: block;
  height: 100%;
  max-width: 100%;
  border-radius: inherit;
  background: currentColor;
}
.kpi-green {
  color: #20aa78;
}
.kpi-blue {
  color: #3779ef;
}
.kpi-orange {
  color: #ef9736;
}
.kpi-cyan {
  color: #20b8b1;
}
.kpi-red {
  color: #e25d67;
}
.overview-primary-grid {
  display: grid;
  grid-template-columns: minmax(560px, 0.94fr) minmax(480px, 1.06fr);
  gap: 14px;
  align-items: stretch;
}
.overview-card {
  min-width: 0;
  border: 1px solid var(--line);
  border-radius: 18px;
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}
.overview-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 54px;
  padding: 13px 16px 10px;
}
.overview-card-head > div {
  display: flex;
  align-items: center;
  gap: 9px;
}
.overview-card-head h3 {
  margin: 0;
  color: #27344e;
  font-size: 13px;
}
.section-icon {
  display: grid;
  place-items: center;
  width: 31px;
  height: 31px;
  border-radius: 9px;
  color: var(--blue);
  background: var(--blue-soft);
}
.overview-card-head button {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 6px;
  border: 0;
  color: var(--blue);
  background: transparent;
  font-size: 10px;
  font-weight: 650;
  cursor: pointer;
}
.pipeline-card {
  min-height: 590px;
  overflow: hidden;
}
.pipeline-orbit {
  position: relative;
  height: 526px;
  min-height: 526px;
  max-width: 620px;
  margin: 0 auto;
  padding: 8px 14px 18px;
  isolation: isolate;
}
.pipeline-orbit-ring {
  position: absolute;
  z-index: -1;
  left: 50%;
  top: 50%;
  width: min(72%, 424px);
  height: min(80%, 424px);
  border: 1px dashed #c9daf4;
  border-radius: 50%;
  background:
    radial-gradient(circle at 50% 50%, rgba(86, 128, 240, 0.09) 0 22%, transparent 23% 100%),
    radial-gradient(circle, rgba(61, 118, 240, 0.04) 0 52%, transparent 53% 100%);
  box-shadow: 0 0 46px rgba(73, 119, 220, 0.07);
  transform: translate(-50%, -50%);
  pointer-events: none;
}
.pipeline-orbit-ring::before,
.pipeline-orbit-ring::after {
  content: '';
  position: absolute;
  border: 1px solid rgba(72, 132, 239, 0.11);
  border-radius: 50%;
}
.pipeline-orbit-ring::before {
  inset: 14% 10%;
}
.pipeline-orbit-ring::after {
  inset: 29% 24%;
  border-style: dashed;
  border-color: rgba(93, 112, 222, 0.15);
}
.pipeline-hub {
  position: absolute;
  z-index: 3;
  left: 50%;
  top: 50%;
  display: grid;
  place-items: center;
  width: 178px;
  height: 178px;
  padding: 16px;
  border: 11px solid rgba(229, 238, 255, 0.96);
  border-radius: 50%;
  background: linear-gradient(145deg, #ffffff, #f3f7ff);
  box-shadow:
    0 18px 42px rgba(51, 91, 158, 0.16),
    0 0 0 1px rgba(207, 222, 247, 0.82),
    inset 0 0 0 1px #d8e5fa;
  text-align: center;
  transform: translate(-50%, -50%);
}
.pipeline-hub-icon {
  display: grid;
  place-items: center;
  width: 42px;
  height: 42px;
  border-radius: 13px;
  color: #ffffff;
  background: linear-gradient(145deg, #4383ff, #655ce7);
  box-shadow: 0 9px 20px rgba(55, 116, 239, 0.24);
}
.pipeline-hub b,
.pipeline-hub small,
.pipeline-hub strong,
.pipeline-hub > span:last-child {
  display: block;
}
.pipeline-hub b {
  margin-top: 5px;
  color: #27344e;
  font-size: 12px;
}
.pipeline-hub small {
  color: #929db0;
  font-size: 8px;
}
.pipeline-hub strong {
  color: #17213f;
  font-size: 27px;
  line-height: 1;
}
.pipeline-hub > span:last-child {
  color: #6f7e96;
  font-size: 8px;
}
.pipeline-orbit-stage {
  position: absolute;
  z-index: 4;
  display: grid;
  grid-template-columns: 40px minmax(0, 1fr) auto;
  gap: 9px;
  align-items: center;
  width: clamp(164px, 30%, 190px);
  min-width: 0;
  min-height: 82px;
  padding: 10px 12px;
  border: 1px solid color-mix(in srgb, currentColor 22%, #e9eef7);
  border-radius: 15px;
  background: linear-gradient(135deg, color-mix(in srgb, currentColor 5%, white), #ffffff 70%);
  box-shadow: 0 10px 26px rgba(53, 81, 121, 0.08);
  transition:
    transform 0.16s ease,
    box-shadow 0.16s ease,
    border-color 0.16s ease;
}
.pipeline-orbit-stage:hover {
  z-index: 6;
  box-shadow: 0 14px 30px rgba(53, 81, 121, 0.13);
}
.pipeline-orbit-stage .pipeline-icon {
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  margin: 0;
  border-radius: 13px;
  color: currentColor;
  background: color-mix(in srgb, currentColor 12%, white);
}
.pipeline-orbit-stage > div {
  min-width: 0;
}
.pipeline-orbit-stage b,
.pipeline-orbit-stage small {
  display: block;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.pipeline-orbit-stage b {
  color: #2b3750;
  font-size: 11px;
}
.pipeline-orbit-stage small {
  margin-top: 4px;
  color: #929db0;
  font-size: 8.5px;
}
.pipeline-orbit-stage strong {
  color: currentColor;
  font-size: 22px;
  line-height: 1;
}
.pipeline-flow-arrows {
  position: absolute;
  z-index: 2;
  inset: 48px 28px 42px;
  width: calc(100% - 56px);
  height: calc(100% - 90px);
  overflow: visible;
  pointer-events: none;
}
.pipeline-flow-arrows path:not([d^='M 0']) {
  fill: none;
  stroke: url(#pipeline-flow-gradient);
  stroke-width: 5.5;
  stroke-linecap: round;
  stroke-linejoin: round;
  marker-end: url(#pipeline-arrowhead);
  opacity: 0.64;
  filter: drop-shadow(0 4px 7px rgba(76, 123, 213, 0.17));
}
.pipeline-flow-arrows marker path {
  fill: #6f9ef3;
}
.orbit-analysis {
  left: 50%;
  top: 8px;
  transform: translateX(-50%);
}
.orbit-search {
  right: 0;
  top: 116px;
}
.orbit-preflight {
  right: 3%;
  bottom: 82px;
}
.orbit-review {
  left: 50%;
  bottom: 10px;
  transform: translateX(-50%);
}
.orbit-execute {
  left: 3%;
  bottom: 82px;
}
.orbit-completed {
  left: 0;
  top: 116px;
}
.orbit-analysis:hover,
.orbit-review:hover {
  transform: translateX(-50%) translateY(-2px);
}
.orbit-search:hover,
.orbit-preflight:hover,
.orbit-execute:hover,
.orbit-completed:hover {
  transform: translateY(-2px);
}
.stage-blue {
  color: #387bef;
}
.stage-cyan {
  color: #2baec8;
}
.stage-purple {
  color: #8266e9;
}
.stage-orange {
  color: #ef9537;
}
.stage-green {
  color: #20aa78;
}
.stage-emerald {
  color: #18a56f;
}
.service-card-head {
  min-height: 76px;
  padding: 14px 18px 8px;
}
.service-heading {
  align-items: flex-start !important;
}
.service-card-head .section-icon {
  width: 42px;
  height: 42px;
  border-radius: 13px;
}
.service-heading-copy {
  display: block !important;
}
.service-heading-copy h3 {
  margin-top: 2px;
  font-size: 15px;
}
.service-heading-copy p {
  margin: 5px 0 0;
  color: #93a0b4;
  font-size: 9.5px;
}
.all-good {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 7px 11px;
  border-radius: 999px;
  color: #20a879;
  background: #eaf8f3;
  font-size: 9.5px;
  font-weight: 750;
  white-space: nowrap;
}
.all-good i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #20a879;
  box-shadow: 0 0 0 4px rgba(32, 168, 121, 0.1);
}
.all-good.warning {
  color: var(--orange);
  background: #fff5e9;
}
.all-good.warning i {
  background: var(--orange);
  box-shadow: 0 0 0 4px rgba(237, 154, 58, 0.1);
}
.services-card {
  display: flex;
  min-height: 590px;
  flex-direction: column;
  overflow: hidden;
}
.service-grid {
  display: grid;
  flex: 1;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  grid-template-rows: repeat(2, minmax(0, 1fr));
  gap: 12px;
  min-height: 0;
  padding: 10px 18px 18px;
}
.service-tile {
  position: relative;
  display: block;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  padding: 19px 20px;
  border: 1px solid #e5edf8;
  border-radius: 17px;
  color: inherit;
  background: linear-gradient(145deg, #ffffff 0%, #fbfdff 72%, #f6f9fe 100%);
  text-align: left;
  cursor: pointer;
  box-shadow: 0 8px 20px rgba(69, 94, 135, 0.035);
  transition:
    transform 0.18s ease,
    border-color 0.18s ease,
    box-shadow 0.18s ease;
}
.service-tile:hover {
  transform: translateY(-2px);
  border-color: #cbd9ef;
  box-shadow: 0 13px 28px rgba(53, 81, 121, 0.08);
}
.service-main {
  position: relative;
  z-index: 2;
  display: grid;
  grid-template-columns: 48px minmax(0, 1fr);
  gap: 13px;
  width: min(72%, 270px);
}
.service-icon,
.quick-icon,
.summary-icon {
  display: grid;
  place-items: center;
  border-radius: 10px;
}
.service-icon {
  width: 48px;
  height: 48px;
  border-radius: 14px;
}
.service-icon.blue,
.quick-icon.blue {
  color: #3678ef;
  background: #eaf2ff;
}
.service-icon.cyan,
.quick-icon.cyan {
  color: #25a9c2;
  background: #e8f8fb;
}
.service-icon.green,
.quick-icon.green,
.summary-icon.green {
  color: #1fa777;
  background: #eaf8f3;
}
.service-icon.purple,
.quick-icon.purple,
.summary-icon.purple {
  color: #7d61e4;
  background: #f1edff;
}
.quick-icon.orange,
.summary-icon.orange {
  color: #ed9435;
  background: #fff3e5;
}
.service-copy {
  min-width: 0;
}
.service-title-row {
  display: flex;
  align-items: center;
  gap: 9px;
  min-height: 27px;
}
.service-tile b {
  color: #202d49;
  font-size: 13px;
  font-weight: 760;
}
.service-title-row small {
  display: inline-flex;
  align-items: center;
  min-height: 23px;
  padding: 3px 9px;
  border-radius: 999px;
  color: #20a879;
  background: #eaf8f3;
  font-size: 8.5px;
  font-weight: 720;
  white-space: nowrap;
}
.service-title-row small.is-warning {
  color: #d98827;
  background: #fff4e7;
}
.service-title-row small.is-critical,
.service-title-row small.is-error {
  color: #df5b67;
  background: #fff0f2;
}
.service-copy strong {
  display: block;
  margin-top: 11px;
  color: #14213d;
  font-size: clamp(20px, 1.7vw, 28px);
  line-height: 1.05;
  white-space: nowrap;
}
.service-value-note {
  display: block;
  margin-top: 6px;
  color: #8794a8;
  font-size: 9.5px;
  white-space: nowrap;
}
.service-copy p {
  margin: 18px 0 0;
  color: #8794a8;
  font-size: 9.5px;
  line-height: 1.4;
  white-space: nowrap;
}
.service-visual {
  position: absolute;
  z-index: 1;
  right: 5%;
  top: 50%;
  display: grid;
  place-items: center;
  width: 112px;
  height: 112px;
  transform: translateY(-42%);
  opacity: 0.16;
  filter: drop-shadow(0 12px 16px rgba(54, 90, 154, 0.08));
  pointer-events: none;
}
.service-visual.blue {
  color: #4d8ef7;
}
.service-visual.cyan {
  color: #35afd0;
}
.service-visual.green {
  color: #49a6d6;
}
.service-visual.purple {
  color: #7b7eea;
}
.service-backups .service-visual {
  opacity: 0.13;
}
.service-updater .service-visual {
  opacity: 0.19;
}
.overview-secondary-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(0, 1fr) minmax(240px, 0.72fr);
  gap: 12px;
  align-items: stretch;
}
.activity-list {
  padding: 0 15px 12px;
}
.activity-list > button {
  display: grid;
  grid-template-columns: 45px 10px minmax(0, 1fr) auto;
  gap: 9px;
  align-items: center;
  width: 100%;
  min-height: 47px;
  padding: 8px 2px;
  border: 0;
  border-bottom: 1px solid #eef2f7;
  color: inherit;
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.activity-list > button:last-child {
  border-bottom: 0;
}
.activity-list time {
  color: #8d98aa;
  font-size: 9px;
}
.activity-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #5f92f4;
}
.tone-success {
  background: var(--green);
}
.tone-warning {
  background: var(--orange);
}
.tone-danger {
  background: var(--red);
}
.activity-list div {
  min-width: 0;
}
.activity-list b,
.activity-list small {
  display: block;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.activity-list b {
  color: #344059;
  font-size: 10px;
}
.activity-list small {
  margin-top: 3px;
  color: #929caf;
  font-size: 8.5px;
}
.quick-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  padding: 5px 13px 14px;
}
.quick-grid > button {
  display: grid;
  justify-items: center;
  min-height: 94px;
  padding: 12px 8px;
  border: 1px solid #edf1f7;
  border-radius: 13px;
  color: inherit;
  background: linear-gradient(180deg, #fff, #fafcff);
  cursor: pointer;
  text-align: center;
  transition:
    transform 0.16s ease,
    box-shadow 0.16s ease;
}
.quick-grid > button:hover {
  transform: translateY(-2px);
  box-shadow: 0 9px 23px rgba(52, 81, 124, 0.07);
}
.quick-icon {
  width: 35px;
  height: 35px;
  margin-bottom: 6px;
}
.quick-grid b {
  color: #34405a;
  font-size: 10px;
}
.quick-grid small {
  margin-top: 3px;
  color: #959fb0;
  font-size: 8px;
  line-height: 1.35;
}
.summary-stack {
  display: grid;
  gap: 9px;
}
.summary-mini {
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr) auto;
  gap: 11px;
  align-items: center;
  min-height: 82px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 17px;
  color: inherit;
  background: var(--surface);
  box-shadow: var(--shadow-sm);
  text-align: left;
  cursor: pointer;
}
.summary-mini {
  width: 100%;
  min-width: 0;
  overflow: hidden;
  box-sizing: border-box;
}
.summary-icon {
  width: 44px;
  height: 44px;
}
.summary-icon {
  flex: 0 0 44px;
  place-items: center;
  overflow: hidden;
  line-height: 0;
}
.summary-icon svg {
  display: block;
  width: 22px;
  height: 22px;
  margin: 0;
}
.summary-mini > svg {
  display: block;
  align-self: center;
  justify-self: end;
  margin: 0;
}
.summary-mini > div {
  min-width: 0;
}
.summary-mini small,
.summary-mini strong,
.summary-mini > div > span {
  display: block;
}
.summary-mini small {
  color: #79859a;
  font-size: 9px;
  font-weight: 700;
}
.summary-mini strong {
  margin: 2px 0;
  color: #1d2944;
  font-size: 19px;
}
.summary-mini span {
  overflow: hidden;
  color: #99a2b2;
  font-size: 8px;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.summary-mini > svg {
  color: #a8b2c2;
}
.health-details {
  border: 1px solid var(--line);
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.76);
}
.health-details > summary {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 16px;
  color: #647189;
  font-size: 11px;
  font-weight: 700;
  cursor: pointer;
  list-style: none;
}
.health-details > summary::-webkit-details-marker {
  display: none;
}
.health-grid-compact {
  padding: 0 14px 14px;
}
.health-grid-compact .health-card {
  border-radius: 13px;
  box-shadow: none;
}
:global(.dark) .overview-hero {
  border-color: #334155;
  background: linear-gradient(110deg, #202b3b, #1c2736 58%, #24344a);
}
:global(.dark) .overview-hero h2,
:global(.dark) .hero-time strong,
:global(.dark) .overview-kpi strong,
:global(.dark) .overview-page-heading h1,
:global(.dark) .pipeline-orbit-stage b,
:global(.dark) .pipeline-hub b,
:global(.dark) .pipeline-hub strong,
:global(.dark) .overview-card-head h3,
:global(.dark) .service-tile b,
:global(.dark) .service-tile strong,
:global(.dark) .activity-list b,
:global(.dark) .quick-grid b,
:global(.dark) .summary-mini strong {
  color: #e6edf8;
}
:global(.dark) .overview-hero-visual {
  background: linear-gradient(145deg, #243a55, #1f3046);
}
:global(.dark) .hero-time,
:global(.dark) .pipeline-orbit-stage,
:global(.dark) .service-tile,
:global(.dark) .quick-grid > button {
  border-color: #334154;
  background: #222e3d;
}
:global(.dark) .pipeline-hub {
  border-color: #29394e;
  background: linear-gradient(145deg, #253346, #1f2c3d);
  box-shadow:
    0 12px 32px rgba(0, 0, 0, 0.18),
    inset 0 0 0 1px #3c4b61;
}
:global(.dark) .pipeline-orbit-ring {
  border-color: #3d526d;
  background: radial-gradient(circle, rgba(72, 132, 239, 0.08) 0 40%, transparent 41% 100%);
}
:global(.dark) .service-tile {
  border-color: #334154;
  background: linear-gradient(145deg, #222e3d 0%, #202b39 72%, #1d2735 100%);
}
:global(.dark) .service-heading-copy p,
:global(.dark) .service-value-note,
:global(.dark) .service-copy p {
  color: #8997aa;
}
:global(.dark) .service-title-row small {
  background: rgba(32, 168, 121, 0.12);
}
:global(.dark) .service-title-row small.is-warning {
  background: rgba(217, 136, 39, 0.14);
}
:global(.dark) .service-title-row small.is-critical,
:global(.dark) .service-title-row small.is-error {
  background: rgba(223, 91, 103, 0.14);
}
:global(.dark) .all-good {
  background: rgba(32, 168, 121, 0.12);
}
:global(.dark) .all-good.warning {
  background: rgba(217, 136, 39, 0.14);
}
:global(.dark) .service-visual {
  opacity: 0.11;
}
:global(.dark) .health-details {
  border-color: #334154;
  background: rgba(32, 40, 52, 0.8);
}
@media (max-width: 1450px) {
  .overview-secondary-grid {
    grid-template-columns: 1fr 1fr;
  }
  .summary-stack {
    grid-column: 1 / -1;
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@media (max-width: 1280px) {
  .overview-primary-grid {
    grid-template-columns: 1fr;
  }
  .pipeline-card {
    min-height: 590px;
  }
  .services-card {
    min-height: auto;
  }
  .service-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    grid-template-rows: repeat(2, minmax(132px, auto));
  }
}
@media (max-width: 1100px) {
  .overview-hero {
    grid-template-columns: 1fr;
  }
  .overview-hero-visual {
    min-height: 125px;
  }
  .overview-primary-grid {
    grid-template-columns: 1fr;
  }
  .pipeline-card {
    min-height: 590px;
  }
  .services-card {
    min-height: auto;
  }
  .service-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .overview-secondary-grid {
    grid-template-columns: 1fr;
  }
  .summary-stack {
    grid-column: auto;
  }
}
@media (max-width: 800px) {
  .overview-page-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 5px;
  }
  .overview-page-heading small {
    white-space: normal;
  }
  .overview-hero-copy {
    padding: 22px 18px;
  }
  .overview-hero h2 {
    font-size: 28px;
  }
  .overview-hero-actions .el-button {
    flex: 1 1 calc(50% - 4px);
    margin: 0;
  }
  .hero-time {
    top: 15px;
    right: 15px;
  }
  .overview-kpis,
  .service-grid,
  .quick-grid,
  .summary-stack {
    grid-template-columns: 1fr;
  }
  .overview-kpi {
    min-height: 132px;
  }
  .pipeline-card {
    min-height: auto;
  }
  .services-card {
    min-height: auto;
  }
  .pipeline-orbit {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 9px;
    height: auto;
    min-height: 0;
    padding: 6px 12px 14px;
  }
  .pipeline-orbit-ring,
  .pipeline-hub,
  .pipeline-flow-arrows {
    display: none;
  }
  .pipeline-orbit-stage {
    position: relative;
    left: auto;
    right: auto;
    top: auto;
    bottom: auto;
    width: auto;
    min-height: 76px;
    transform: none;
  }
  .pipeline-orbit-stage:hover,
  .orbit-analysis:hover,
  .orbit-review:hover,
  .orbit-search:hover,
  .orbit-preflight:hover,
  .orbit-execute:hover,
  .orbit-completed:hover {
    transform: translateY(-2px);
  }
  .service-grid {
    grid-template-rows: none;
  }
  .service-tile {
    min-height: 112px;
  }
  .service-main {
    width: min(76%, 320px);
  }
  .service-copy p {
    margin-top: 12px;
  }
  .service-visual {
    right: 3%;
    transform: translateY(-44%) scale(0.86);
  }
  .activity-list > button {
    grid-template-columns: 38px 8px minmax(0, 1fr);
  }
  .activity-list .el-tag {
    grid-column: 3;
    width: max-content;
  }
  .health-grid-compact {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 560px) {
  .pipeline-orbit {
    grid-template-columns: 1fr;
  }
  .service-card-head {
    align-items: flex-start;
  }
  .all-good {
    padding: 6px 9px;
    font-size: 8.5px;
  }
  .service-grid {
    gap: 9px;
    padding: 8px 12px 14px;
  }
  .service-tile {
    min-height: 150px;
    padding: 16px;
  }
  .service-main {
    grid-template-columns: 42px minmax(0, 1fr);
    gap: 11px;
    width: min(82%, 300px);
  }
  .service-icon {
    width: 42px;
    height: 42px;
  }
  .service-copy strong {
    font-size: 21px;
  }
  .service-copy p {
    overflow: hidden;
    max-width: 180px;
    margin-top: 10px;
    white-space: nowrap;
    text-overflow: ellipsis;
  }
  .service-visual {
    right: -3%;
    transform: translateY(-42%) scale(0.7);
    opacity: 0.1;
  }
}
</style>
