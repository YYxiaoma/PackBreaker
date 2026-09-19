<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import {
  Activity,
  ArrowRight,
  Bell,
  CheckCircle2,
  CircleAlert,
  Cpu,
  FileCheck2,
  FileText,
  Globe,
  HardDrive,
  Link2,
  Play,
  RefreshCw,
  ShieldCheck,
  Zap,
} from '@lucide/vue';
import { getSystemHealth, type SystemHealth, type SystemHealthCheck } from '../api/system';

const emit = defineEmits<{ navigate: [page: string] }>();
const health = ref<SystemHealth | null>(null);
const busy = ref(false);
const loadError = ref('');
const cpuHistory = ref<number[]>([]);
const cpuBars = computed<(number | null)[]>(() => [
  ...Array<number | null>(Math.max(0, 12 - cpuHistory.value.length)).fill(null),
  ...cpuHistory.value,
]);
let timer: ReturnType<typeof setInterval> | undefined;

function check(name: string): SystemHealthCheck | undefined {
  return health.value?.checks.find((item) => item.name === name);
}
function metric(checkName: string, key: string): number | null {
  const value = check(checkName)?.metrics[key];
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}
function count(checkName: string, key: string): string {
  return metric(checkName, key)?.toLocaleString('zh-CN') ?? '—';
}
function bytes(value: number | null): string {
  if (value === null) return '—';
  if (value >= 1024 ** 4) return `${(value / 1024 ** 4).toFixed(1)} TiB`;
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} GiB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(0)} MiB`;
  return `${value.toLocaleString('zh-CN')} B`;
}
function ratio(numerator: number | null, denominator: number | null): number | null {
  if (numerator === null || denominator === null || denominator <= 0) return null;
  return Math.round((numerator / denominator) * 100);
}
function percent(value: number | null): string {
  return value === null ? '—' : `${value}%`;
}
function progress(value: number | null): string {
  return `${Math.min(100, Math.max(0, value ?? 0))}%`;
}
function status(checkName: string): string {
  const item = check(checkName);
  if (!item) return '暂无数据';
  return { ok: '正常', warning: '需关注', blocked: '异常' }[item.status];
}
function dotClass(checkName: string): string {
  return check(checkName)?.status ?? 'unavailable';
}

const diskTotal = computed(() => metric('storage', 'data_total_bytes'));
const diskFree = computed(() => metric('storage', 'data_free_bytes'));
const diskUsed = computed(() =>
  diskTotal.value !== null && diskFree.value !== null
    ? Math.max(0, diskTotal.value - diskFree.value)
    : null,
);
const diskPercent = computed(() => ratio(diskUsed.value, diskTotal.value));
const memoryTotal = computed(() => metric('resources', 'memory_total_bytes'));
const memoryFree = computed(() => metric('resources', 'memory_available_bytes'));
const memoryUsed = computed(() =>
  memoryTotal.value !== null && memoryFree.value !== null
    ? Math.max(0, memoryTotal.value - memoryFree.value)
    : null,
);
const memoryPercent = computed(() => ratio(memoryUsed.value, memoryTotal.value));
const cpuLoad = computed(() => metric('resources', 'cpu_load_percent'));
const downloaderTotal = computed(() => metric('downloaders', 'configured'));
const downloaderConnected = computed(() => metric('downloaders', 'connection_ok'));
const siteTotal = computed(() => metric('sites', 'configured'));
const siteConnected = computed(() => metric('sites', 'connection_ok'));
const downloaderPercent = computed(() => ratio(downloaderConnected.value, downloaderTotal.value));
const sitePercent = computed(() => ratio(siteConnected.value, siteTotal.value));

async function refresh(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  try {
    const next = await getSystemHealth();
    health.value = next;
    loadError.value = '';
    const current = next.checks.find((item) => item.name === 'resources')?.metrics.cpu_load_percent;
    if (typeof current === 'number' && Number.isFinite(current) && current >= 0) {
      cpuHistory.value = [...cpuHistory.value.slice(-11), current];
    }
  } catch {
    loadError.value = health.value
      ? '本次刷新失败，以下为上次成功获取的数据'
      : '系统状态暂不可用，请检查网络或稍后重试';
  } finally {
    busy.value = false;
  }
}
onMounted(() => {
  void refresh();
  timer = setInterval(() => void refresh(), 30_000);
});
onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>

<template>
  <div class="overview-dashboard" :aria-busy="busy">
    <header class="overview-heading">
      <div>
        <h1>PackBreaker 总览</h1>
        <p>统一管理任务、审批、执行与校验</p>
      </div>
      <div class="overview-mountain" aria-hidden="true">让获取更简单</div>
    </header>

    <div v-if="loadError" class="overview-alert" role="status">
      <CircleAlert :size="16" />{{ loadError }}
      <button type="button" @click="refresh">重试</button>
    </div>
    <div v-if="health && !loadError" class="overview-timestamp">
      数据更新于 {{ new Date(health.generated_at).toLocaleString('zh-CN') }}
      <button type="button" aria-label="刷新总览" :disabled="busy" @click="refresh">
        <RefreshCw :size="15" />
      </button>
    </div>

    <section class="overview-kpi-grid" aria-label="任务与连接摘要">
      <button class="overview-card kpi-card" type="button" @click="emit('navigate', '任务中心')">
        <span class="metric-icon orange"><Bell :size="25" /></span>
        <span class="kpi-content"
          ><span>待审批</span><strong>{{ count('tasks', 'awaiting_confirmation') }}</strong>
          <small>个任务等待确认</small></span
        >
        <ArrowRight :size="16" class="card-arrow" />
      </button>
      <button class="overview-card kpi-card" type="button" @click="emit('navigate', '任务中心')">
        <span class="metric-icon blue"><Play :size="25" /></span>
        <span class="kpi-content"
          ><span>运行中任务</span><strong>{{ count('tasks', 'running') }}</strong>
          <small>个任务正在执行</small></span
        >
        <ArrowRight :size="16" class="card-arrow" />
      </button>
      <button class="overview-card kpi-card" type="button" @click="emit('navigate', '站点管理')">
        <span class="metric-icon green"><Globe :size="25" /></span>
        <span class="kpi-content"
          ><span>站点状态</span><strong>{{ count('sites', 'connection_ok') }}</strong>
          <small>个已验证可用（共 {{ count('sites', 'configured') }} 个）</small></span
        >
        <ArrowRight :size="16" class="card-arrow" />
      </button>
      <button class="overview-card kpi-card" type="button" @click="emit('navigate', '下载器')">
        <span class="metric-icon purple"><HardDrive :size="25" /></span>
        <span class="kpi-content"
          ><span>下载器状态</span><strong>{{ count('downloaders', 'connection_ok') }}</strong>
          <small>个已验证连接（共 {{ count('downloaders', 'configured') }} 个）</small></span
        >
        <ArrowRight :size="16" class="card-arrow" />
      </button>
    </section>

    <section class="overview-monitor-grid" aria-label="系统资源与连接状态">
      <article class="overview-card monitor-card">
        <div class="monitor-heading">
          <span class="metric-icon green"><CheckCircle2 :size="24" /></span>
          <div>
            <h2>系统状态</h2>
            <strong :class="health?.status === 'ok' ? 'healthy' : 'attention'">
              {{
                !health
                  ? '暂无数据'
                  : health.status === 'ok'
                    ? '运行正常'
                    : health.status === 'warning'
                      ? '需要关注'
                      : '运行异常'
              }}
            </strong>
          </div>
        </div>
        <div class="service-list">
          <div :class="loadError || !health ? 'unavailable' : 'ok'">
            <span>Web 服务</span
            ><b :class="loadError ? 'unavailable' : health ? 'ok' : 'unavailable'">{{
              loadError ? '状态未更新' : health ? '正常' : '未知'
            }}</b>
          </div>
          <div :class="dotClass('workers')">
            <span>调度服务</span><b :class="dotClass('workers')">{{ status('workers') }}</b>
          </div>
          <div :class="dotClass('storage')">
            <span>存储服务</span><b :class="dotClass('storage')">{{ status('storage') }}</b>
          </div>
          <div :class="dotClass('runtime')">
            <span>数据库</span><b :class="dotClass('runtime')">{{ status('runtime') }}</b>
          </div>
        </div>
      </article>

      <article class="overview-card monitor-card">
        <div class="monitor-heading">
          <span class="metric-icon blue"><HardDrive :size="24" /></span>
          <div>
            <h2>磁盘空间</h2>
            <strong
              >{{ bytes(diskUsed) }} <small>/ {{ bytes(diskTotal) }}</small></strong
            >
          </div>
        </div>
        <div class="bar-row">
          <div class="progress">
            <span class="fill blue-fill" :style="{ width: progress(diskPercent) }"></span>
          </div>
          <b>{{ percent(diskPercent) }}</b>
        </div>
        <dl>
          <dt>已使用</dt>
          <dd>{{ bytes(diskUsed) }}</dd>
          <dt>可用空间</dt>
          <dd>{{ bytes(diskFree) }}</dd>
          <dt>总容量</dt>
          <dd>{{ bytes(diskTotal) }}</dd>
        </dl>
        <small class="footnote">数据目录所在磁盘</small>
      </article>

      <article class="overview-card monitor-card">
        <div class="monitor-heading">
          <span class="metric-icon purple"><Cpu :size="24" /></span>
          <div>
            <h2>内存占用</h2>
            <strong
              >{{ bytes(memoryUsed) }} <small>/ {{ bytes(memoryTotal) }}</small></strong
            >
          </div>
        </div>
        <div class="bar-row">
          <div class="progress">
            <span class="fill purple-fill" :style="{ width: progress(memoryPercent) }"></span>
          </div>
          <b>{{ percent(memoryPercent) }}</b>
        </div>
        <dl>
          <dt>已使用</dt>
          <dd>{{ bytes(memoryUsed) }}</dd>
          <dt>可用内存</dt>
          <dd>{{ bytes(memoryFree) }}</dd>
          <dt>总内存</dt>
          <dd>{{ bytes(memoryTotal) }}</dd>
        </dl>
        <small class="footnote">优先显示容器内存限额</small>
      </article>

      <article class="overview-card monitor-card compact-card">
        <div class="monitor-heading">
          <span class="metric-icon green"><Activity :size="24" /></span>
          <div>
            <h2>CPU 负载</h2>
            <strong>{{ cpuLoad === null ? '—' : `${cpuLoad.toFixed(1)}%` }}</strong>
          </div>
        </div>
        <div class="cpu-bars" aria-hidden="true">
          <span
            v-for="(sample, index) in cpuBars"
            :key="index"
            :class="{ 'is-pending': sample === null }"
            :style="{ height: sample === null ? '5px' : `${Math.max(5, Math.min(100, sample))}%` }"
          ></span>
        </div>
        <small class="footnote">近 1 分钟系统负载 / 逻辑 CPU 数 · 每 30 秒采样</small>
      </article>

      <article class="overview-card monitor-card compact-card">
        <div class="monitor-heading">
          <span class="metric-icon blue"><Link2 :size="24" /></span>
          <div>
            <h2>下载器连接</h2>
            <strong
              >{{ count('downloaders', 'connection_ok') }} /
              {{ count('downloaders', 'configured') }}</strong
            >
          </div>
        </div>
        <div class="bar-row">
          <div class="progress">
            <span class="fill green-fill" :style="{ width: progress(downloaderPercent) }"></span>
          </div>
          <b>{{ percent(downloaderPercent) }}</b>
        </div>
        <small class="footnote">已启用且最近连接验证成功 · 非实时探测</small>
      </article>

      <article class="overview-card monitor-card compact-card">
        <div class="monitor-heading">
          <span class="metric-icon blue"><Globe :size="24" /></span>
          <div>
            <h2>站点在线情况</h2>
            <strong
              >{{ count('sites', 'connection_ok') }} / {{ count('sites', 'configured') }}</strong
            >
          </div>
        </div>
        <div class="bar-row">
          <div class="progress">
            <span class="fill blue-fill" :style="{ width: progress(sitePercent) }"></span>
          </div>
          <b>{{ percent(sitePercent) }}</b>
        </div>
        <small class="footnote">已启用且最近连接验证成功 · 非实时探测</small>
      </article>
    </section>

    <section class="overview-card lifecycle" aria-label="统一的任务生命周期">
      <div class="lifecycle-heading">
        <h2><Zap :size="22" />统一的任务生命周期</h2>
        <small>从任务定义到校验完成，简单、可控、更可靠。</small>
      </div>
      <div class="lifecycle-steps">
        <div class="life-step">
          <span class="metric-icon blue"><FileText :size="24" /></span><b>任务定义</b>
        </div>
        <ArrowRight :size="21" class="life-arrow" />
        <div class="life-step">
          <span class="metric-icon purple"><ShieldCheck :size="24" /></span><b>风险检查</b>
        </div>
        <ArrowRight :size="21" class="life-arrow" />
        <div class="life-step">
          <span class="metric-icon green"><CheckCircle2 :size="24" /></span><b>审批确认</b>
        </div>
        <ArrowRight :size="21" class="life-arrow" />
        <div class="life-step">
          <span class="metric-icon blue"><Play :size="24" /></span><b>执行 / 重试</b>
        </div>
        <ArrowRight :size="21" class="life-arrow" />
        <div class="life-step">
          <span class="metric-icon blue"><FileCheck2 :size="24" /></span><b>校验 / 对账</b>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.overview-dashboard {
  display: grid;
  gap: 16px;
  min-width: 0;
  color: var(--ink);
}
.overview-heading {
  min-height: 112px;
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  overflow: hidden;
  padding: 8px 12px;
}
.overview-heading h1 {
  font-size: clamp(25px, 2.4vw, 35px);
  line-height: 1.2;
  margin: 0 0 10px;
  font-weight: 750;
  letter-spacing: -0.7px;
}
.overview-heading p {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
}
.overview-mountain {
  align-self: end;
  min-width: 270px;
  height: 76px;
  display: grid;
  place-items: center;
  color: #6894ce;
  font-style: italic;
  background:
    linear-gradient(150deg, transparent 35%, rgba(170, 201, 249, 0.18) 35% 60%, transparent 61%),
    linear-gradient(170deg, transparent 20%, rgba(149, 190, 246, 0.19) 20% 69%, transparent 70%);
  border-radius: 80% 40% 0 0;
  opacity: 0.84;
}
.overview-alert,
.overview-timestamp {
  display: flex;
  align-items: center;
  gap: 9px;
  font-size: 12px;
  color: var(--muted);
}
.overview-alert {
  color: #b65b20;
}
.overview-alert button,
.overview-timestamp button {
  border: 0;
  background: transparent;
  color: var(--blue);
  cursor: pointer;
}
.overview-timestamp {
  justify-content: flex-end;
  min-height: 16px;
  margin-top: -9px;
}
.overview-kpi-grid,
.overview-monitor-grid {
  display: grid;
  gap: 14px;
  min-width: 0;
}
.overview-kpi-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}
.overview-monitor-grid {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.overview-card {
  border: 1px solid var(--line);
  background: var(--surface);
  border-radius: 14px;
  box-shadow: 0 6px 22px rgba(33, 83, 158, 0.055);
  min-width: 0;
}
.kpi-card {
  display: flex;
  align-items: center;
  text-align: left;
  gap: 14px;
  padding: 22px 17px;
  min-height: 145px;
  color: inherit;
  cursor: pointer;
  transition:
    border-color 0.2s,
    transform 0.2s;
}
.kpi-card:hover {
  border-color: #91b6f3;
  transform: translateY(-1px);
}
.metric-icon {
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  width: 52px;
  height: 52px;
  border-radius: 50%;
}
.metric-icon.orange {
  color: #e88926;
  background: #fff3e5;
}
.metric-icon.blue {
  color: #2872df;
  background: #eaf3ff;
}
.metric-icon.green {
  color: #0da66b;
  background: #e8f9ef;
}
.metric-icon.purple {
  color: #7954ed;
  background: #f0ebff;
}
.kpi-content {
  display: grid;
  gap: 4px;
  min-width: 0;
}
.kpi-content > span {
  font-weight: 700;
  font-size: 13px;
}
.kpi-content strong {
  font-size: 28px;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
}
.kpi-content small {
  font-size: 11px;
  color: var(--muted);
  line-height: 1.4;
}
.card-arrow {
  margin-left: auto;
  color: #9eafc9;
  flex: 0 0 auto;
}
.monitor-card {
  padding: 18px 20px;
  min-height: 210px;
  display: flex;
  flex-direction: column;
  gap: 13px;
}
.monitor-heading {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
}
.monitor-heading > div {
  min-width: 0;
}
.monitor-heading h2 {
  margin: 0 0 7px;
  font-size: 14px;
  font-weight: 700;
}
.monitor-heading strong {
  font-size: 22px;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
}
.monitor-heading strong small {
  color: var(--muted);
  font-size: 12px;
  font-weight: 500;
}
.monitor-heading strong.healthy {
  color: #07965e;
}
.monitor-heading strong.attention {
  color: #d18a32;
}
.service-list {
  display: grid;
  gap: 6px;
  padding: 10px 14px;
  border-radius: 9px;
  background: rgba(52, 194, 119, 0.065);
}
.service-list > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
}
.service-list > div span::before {
  content: '';
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #23b579;
  margin-right: 9px;
}
.service-list > div.warning span::before,
.service-list > div.unavailable span::before {
  background: #d7a25e;
}
.service-list > div.blocked span::before {
  background: #d95761;
}
.service-list b {
  font-size: 11px;
  color: #07965e;
}
.service-list b.warning,
.service-list b.unavailable {
  color: #b98333;
}
.service-list b.blocked {
  color: #d24b52;
}
.bar-row {
  display: flex;
  align-items: center;
  gap: 11px;
}
.bar-row > b {
  min-width: 35px;
  text-align: right;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.progress {
  height: 10px;
  flex: 1;
  background: #e5edfa;
  border-radius: 99px;
  overflow: hidden;
}
.fill {
  display: block;
  height: 100%;
  border-radius: 99px;
  transition: width 0.3s;
}
.blue-fill {
  background: #3384f0;
}
.purple-fill {
  background: #8c5cf2;
}
.green-fill {
  background: #13b67b;
}
.monitor-card dl {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 7px;
  margin: 0;
  color: var(--muted);
  font-size: 11px;
}
.monitor-card dt::before {
  content: '•';
  color: #6b9df1;
  margin-right: 7px;
}
.monitor-card dd {
  margin: 0;
  font-variant-numeric: tabular-nums;
}
.footnote {
  margin-top: auto;
  font-size: 10px;
  line-height: 1.5;
  color: var(--muted);
}
.compact-card {
  min-height: 151px;
}
.compact-card .bar-row {
  margin-top: auto;
}
.cpu-bars {
  display: flex;
  align-items: end;
  gap: 7px;
  height: 42px;
  padding: 2px 0;
}
.cpu-bars span {
  flex: 1;
  max-width: 20px;
  min-width: 5px;
  border-radius: 3px 3px 0 0;
  background: linear-gradient(#0dba79, #91dfba);
}
.cpu-bars span.is-pending {
  background: var(--line);
}
.lifecycle {
  padding: 18px 24px 20px;
}
.lifecycle-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  margin-bottom: 20px;
}
.lifecycle-heading h2 {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  margin: 0;
  font-size: 15px;
}
.lifecycle-heading h2 svg {
  color: #287be8;
  fill: #287be8;
}
.lifecycle-heading > small {
  color: var(--muted);
  font-size: 11px;
}
.lifecycle-steps {
  display: flex;
  align-items: center;
  justify-content: space-around;
  gap: 10px;
}
.life-step {
  display: grid;
  justify-items: center;
  gap: 9px;
  text-align: center;
  min-width: 0;
}
.life-step b {
  font-size: 12px;
  white-space: nowrap;
}
.life-arrow {
  color: #99afcd;
  flex: 0 0 auto;
}
:global(.dark) .overview-heading h1,
:global(.dark) .overview-dashboard {
  color: #e5edf9;
}
:global(.dark) .overview-card {
  box-shadow: none;
}
:global(.dark) .service-list {
  background: rgba(52, 194, 119, 0.08);
}
:global(.dark) .progress {
  background: #334055;
}
@media (max-width: 1180px) {
  .overview-kpi-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .overview-monitor-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 720px) {
  .overview-heading {
    min-height: 90px;
  }
  .overview-mountain {
    display: none;
  }
  .overview-monitor-grid {
    grid-template-columns: 1fr;
  }
  .lifecycle-steps {
    flex-wrap: wrap;
    justify-content: center;
    gap: 18px;
  }
  .life-arrow {
    display: none;
  }
  .lifecycle-heading {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }
  .life-step {
    width: 28%;
  }
}
@media (max-width: 460px) {
  .overview-kpi-grid {
    gap: 9px;
  }
  .kpi-card {
    min-height: 120px;
    padding: 12px;
    gap: 8px;
    align-items: flex-start;
  }
  .kpi-card .metric-icon {
    width: 34px;
    height: 34px;
  }
  .kpi-card .metric-icon svg {
    width: 19px;
  }
  .kpi-content strong {
    font-size: 23px;
  }
  .kpi-content > span {
    font-size: 11px;
  }
  .kpi-content small {
    font-size: 10px;
  }
  .card-arrow {
    display: none;
  }
  .monitor-card {
    padding: 16px;
  }
  .life-step {
    width: 42%;
  }
}
</style>
