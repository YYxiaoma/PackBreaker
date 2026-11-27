<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { Download, RefreshCw } from '@lucide/vue';
import { ElMessage } from 'element-plus';
import * as echarts from 'echarts/core';
import { BarChart, LineChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import {
  exportSiteAssetCsv,
  getSiteAssetSummary,
  getSiteAssetTrend,
  listSiteAssetSites,
  type SiteAssetPoint,
  type SiteAssetSite,
  type SiteAssetSummary,
} from '../api/siteAssets';
import { toApiProblem } from '../api/client';

echarts.use([
  LineChart,
  BarChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  CanvasRenderer,
]);

const props = defineProps<{ dark: boolean }>();

const days = ref(30);
const search = ref('');
const page = ref(1);
const pageSize = ref(20);
const total = ref(0);
const loading = ref(false);
const exporting = ref(false);
const errorText = ref('');
const summary = ref<SiteAssetSummary | null>(null);
const points = ref<SiteAssetPoint[]>([]);
const sites = ref<SiteAssetSite[]>([]);

const trendEl = ref<HTMLDivElement | null>(null);
const ratioEl = ref<HTMLDivElement | null>(null);
let trendChart: ReturnType<typeof echarts.init> | null = null;
let ratioChart: ReturnType<typeof echarts.init> | null = null;
let observer: ResizeObserver | null = null;
let requestSequence = 0;

function formatNumber(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return value.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function formatRatio(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value) ? '—' : value.toFixed(2);
}

function formatTime(value: string | null | undefined): string {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleString('zh-CN', { hour12: false });
}

function changeLabel(metric: string): string {
  const value = summary.value?.changes?.[metric];
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  const digits = metric === 'ratio' ? 2 : 0;
  const sign = value > 0 ? '+' : '';
  return `${sign}${formatNumber(value, digits)}`;
}

function ensureCharts(): void {
  if (trendEl.value && !trendChart) trendChart = echarts.init(trendEl.value);
  if (ratioEl.value && !ratioChart) ratioChart = echarts.init(ratioEl.value);
}

function chartColors() {
  return props.dark
    ? { text: '#e5e7eb', muted: '#9ca3af', grid: '#3f3f46', tooltip: '#27272a' }
    : { text: '#2c2c2a', muted: '#6b7280', grid: '#e5e7eb', tooltip: '#ffffff' };
}

function updateCharts(): void {
  ensureCharts();
  if (!trendChart || !ratioChart) return;
  const colors = chartColors();
  const dates = points.value.map((item) => item.stat_date);

  trendChart.setOption(
    {
      backgroundColor: 'transparent',
      color: ['#185FA5', '#0F6E56', '#BA7517', '#993C1D'],
      textStyle: { color: colors.text },
      tooltip: {
        trigger: 'axis',
        backgroundColor: colors.tooltip,
        borderColor: colors.grid,
        textStyle: { color: colors.text },
      },
      legend: {
        top: 0,
        textStyle: { color: colors.muted },
        data: ['分享率', '做种数', '魔力值/积分', '时魔'],
      },
      grid: { left: 48, right: 58, top: 38, bottom: 30 },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: dates,
        axisLine: { lineStyle: { color: colors.grid } },
        axisLabel: { color: colors.muted, hideOverlap: true },
      },
      yAxis: [
        {
          type: 'value',
          name: 'ratio / 做种数',
          nameTextStyle: { color: colors.muted, fontSize: 11 },
          axisLabel: { color: colors.muted },
          splitLine: { lineStyle: { color: colors.grid } },
        },
        {
          type: 'value',
          name: 'bonus / 时魔',
          nameTextStyle: { color: colors.muted, fontSize: 11 },
          axisLabel: { color: colors.muted },
          splitLine: { show: false },
        },
      ],
      series: [
        {
          name: '分享率',
          type: 'line',
          yAxisIndex: 0,
          showSymbol: false,
          connectNulls: false,
          data: points.value.map((item) => item.ratio),
        },
        {
          name: '做种数',
          type: 'line',
          yAxisIndex: 0,
          showSymbol: false,
          connectNulls: false,
          data: points.value.map((item) => item.seeding_count),
        },
        {
          name: '魔力值/积分',
          type: 'line',
          yAxisIndex: 1,
          showSymbol: false,
          connectNulls: false,
          data: points.value.map((item) => item.bonus),
        },
        {
          name: '时魔',
          type: 'line',
          yAxisIndex: 1,
          showSymbol: false,
          connectNulls: false,
          data: points.value.map((item) => item.bonus_per_hour),
        },
      ],
    },
    true,
  );

  const distribution = summary.value?.ratio_distribution ?? [];
  ratioChart.setOption(
    {
      backgroundColor: 'transparent',
      textStyle: { color: colors.text },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        backgroundColor: colors.tooltip,
        borderColor: colors.grid,
        textStyle: { color: colors.text },
        valueFormatter: (value: unknown) => formatRatio(value as number),
      },
      grid: { left: 78, right: 24, top: 12, bottom: 20 },
      xAxis: {
        type: 'value',
        axisLabel: { color: colors.muted },
        splitLine: { lineStyle: { color: colors.grid } },
      },
      yAxis: {
        type: 'category',
        data: distribution.map((item) => item.site_name).reverse(),
        axisLabel: { color: colors.muted, width: 72, overflow: 'truncate' },
        axisLine: { lineStyle: { color: colors.grid } },
      },
      series: [
        {
          type: 'bar',
          data: distribution.map((item) => item.ratio).reverse(),
          itemStyle: { color: '#378ADD', borderRadius: [0, 4, 4, 0] },
          barMaxWidth: 16,
        },
      ],
    },
    true,
  );
}

async function loadSites(): Promise<void> {
  try {
    const result = await listSiteAssetSites(days.value, {
      query: search.value,
      page: page.value,
      pageSize: pageSize.value,
    });
    sites.value = result.items;
    total.value = result.total;
    errorText.value = '';
  } catch (error) {
    errorText.value = toApiProblem(error).message;
  }
}

async function refresh(): Promise<void> {
  const sequence = ++requestSequence;
  loading.value = true;
  errorText.value = '';
  try {
    const [nextSummary, nextPoints, nextSites] = await Promise.all([
      getSiteAssetSummary(days.value),
      getSiteAssetTrend(days.value),
      listSiteAssetSites(days.value, { page: 1, pageSize: pageSize.value }),
    ]);
    if (sequence !== requestSequence) return;
    summary.value = nextSummary;
    points.value = nextPoints;
    sites.value = nextSites.items;
    total.value = nextSites.total;
    page.value = 1;
    await nextTick();
    updateCharts();
  } catch (error) {
    if (sequence === requestSequence) errorText.value = toApiProblem(error).message;
  } finally {
    if (sequence === requestSequence) loading.value = false;
  }
}

async function exportCsv(): Promise<void> {
  exporting.value = true;
  try {
    const { blob, filename } = await exportSiteAssetCsv(days.value);
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    URL.revokeObjectURL(url);
    ElMessage.success('报表已导出');
  } catch (error) {
    ElMessage.error(toApiProblem(error).message);
  } finally {
    exporting.value = false;
  }
}

function handleResize(): void {
  trendChart?.resize();
  ratioChart?.resize();
}

watch(
  () => props.dark,
  () => updateCharts(),
);

onMounted(async () => {
  ensureCharts();
  observer = new ResizeObserver(handleResize);
  if (trendEl.value) observer.observe(trendEl.value);
  if (ratioEl.value) observer.observe(ratioEl.value);
  await refresh();
});

onBeforeUnmount(() => {
  observer?.disconnect();
  trendChart?.dispose();
  ratioChart?.dispose();
});
</script>

<template>
  <section class="site-assets-panel">
    <header class="page-head">
      <div>
        <h1>站点资产趋势</h1>
        <p>PT 资产总览 · 所有站点的分享率分布、魔力值总量与时魔总和</p>
      </div>
      <div class="head-actions">
        <div class="window-switch" role="group" aria-label="时间窗口">
          <button
            v-for="option in [7, 30, 90]"
            :key="option"
            type="button"
            :class="{ active: days === option }"
            @click="
              days = option;
              refresh();
            "
          >
            {{ option }}天
          </button>
        </div>
        <el-button :loading="exporting" :disabled="loading" @click="exportCsv">
          <Download :size="15" /> 导出报表
        </el-button>
        <el-button :loading="loading" @click="refresh"> <RefreshCw :size="15" /> 刷新 </el-button>
      </div>
    </header>

    <div v-if="errorText" class="banner danger" role="alert">{{ errorText }}</div>
    <div v-if="summary && summary.incomplete_sites.length" class="banner warning" role="status">
      最新日期仅覆盖
      {{ summary?.site_count ?? 0 }}
      个站点，当日合计为部分站点数据；以下站点没有快照，曲线未把它们当作 0：{{
        summary.incomplete_sites.join('、')
      }}
    </div>

    <div class="stat-grid">
      <article class="stat-card">
        <span>魔力值 / 积分总量</span>
        <strong>{{ formatNumber(summary?.bonus_total, 0) }}</strong>
        <small>较窗口首日 {{ changeLabel('bonus') }}</small>
      </article>
      <article class="stat-card">
        <span>时魔总和（每小时）</span>
        <strong>{{ formatNumber(summary?.bonus_per_hour_total, 1) }}</strong>
        <small>较窗口首日 {{ changeLabel('bonus_per_hour') }}</small>
      </article>
      <article class="stat-card">
        <span>做种总数</span>
        <strong>{{ formatNumber(summary?.seeding_count_total, 0) }}</strong>
        <small>较窗口首日 {{ changeLabel('seeding_count') }}</small>
      </article>
      <article class="stat-card">
        <span>参与统计站点</span>
        <strong>{{ summary?.site_count ?? 0 }}</strong>
        <small>有快照的站点数</small>
      </article>
    </div>

    <div class="chart-grid">
      <section class="chart-card">
        <div class="card-title">
          <h2>趋势曲线</h2>
          <span>缺失日期不补零，曲线断开显示</span>
        </div>
        <div ref="trendEl" class="chart trend-chart"></div>
        <p v-if="!points.length && !loading" class="chart-empty">
          历史数据从启用后开始积累，暂无可绘制的趋势点。
        </p>
      </section>
      <section class="chart-card">
        <div class="card-title">
          <h2>分享率分布</h2>
          <span>按最新快照降序</span>
        </div>
        <div ref="ratioEl" class="chart ratio-chart"></div>
      </section>
    </div>

    <section class="table-card">
      <div class="filter-bar">
        <el-input
          v-model="search"
          placeholder="搜索站点名称"
          clearable
          class="search-input"
          @keyup.enter="
            page = 1;
            loadSites();
          "
        />
        <el-button
          @click="
            page = 1;
            loadSites();
          "
          >查询</el-button
        >
      </div>
      <div class="table-scroll">
        <el-table :data="sites" v-loading="loading" row-key="site_id" empty-text=" ">
          <el-table-column label="站点" min-width="180">
            <template #default="{ row }">
              <div class="site-cell">
                <strong>{{ row.site_name }}</strong>
                <span>{{ row.site_type }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="分享率" min-width="140">
            <template #default="{ row }">
              <strong>{{ formatRatio(row.ratio) }}</strong>
              <small>{{
                row.ratio_change === null ? '—' : formatNumber(row.ratio_change, 2)
              }}</small>
            </template>
          </el-table-column>
          <el-table-column label="魔力值/积分" min-width="140">
            <template #default="{ row }">{{ formatNumber(row.bonus, 0) }}</template>
          </el-table-column>
          <el-table-column label="时魔" min-width="120">
            <template #default="{ row }">{{ formatNumber(row.bonus_per_hour, 1) }}</template>
          </el-table-column>
          <el-table-column label="做种数" min-width="110">
            <template #default="{ row }">{{ formatNumber(row.seeding_count, 0) }}</template>
          </el-table-column>
          <el-table-column label="同步时间" min-width="170">
            <template #default="{ row }">{{ formatTime(row.fetched_at) }}</template>
          </el-table-column>
        </el-table>
      </div>
      <p v-if="!sites.length && !loading" class="table-empty">当前窗口没有站点快照。</p>
      <div class="pagination">
        <el-pagination
          layout="prev, pager, next"
          :total="total"
          :page-size="pageSize"
          :current-page="page"
          @current-change="
            page = $event;
            loadSites();
          "
        />
      </div>
    </section>
  </section>
</template>

<style scoped>
.site-assets-panel {
  display: grid;
  gap: 18px;
  width: 100%;
  min-width: 0;
  max-width: 100%;
  padding: 24px 28px 40px;
  box-sizing: border-box;
  overflow-x: hidden;
}
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
}
h1 {
  margin: 0;
  font-size: 25px;
  font-weight: 600;
}
.page-head p {
  margin: 6px 0 0;
  color: var(--muted);
  font-size: 13px;
}
.head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.window-switch {
  display: inline-flex;
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}
.window-switch button {
  border: 0;
  background: var(--surface);
  color: var(--muted);
  padding: 7px 12px;
  cursor: pointer;
  font-size: 13px;
}
.window-switch button.active {
  background: var(--blue-soft);
  color: var(--blue);
  font-weight: 600;
}
.banner {
  border-radius: 8px;
  padding: 10px 14px;
  font-size: 13px;
}
.banner.warning {
  background: var(--warning-soft, #fff7e6);
  color: var(--warning, #9a6700);
}
.banner.danger {
  background: var(--danger-soft, #fef0f0);
  color: var(--danger, #b42318);
}
.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}
.stat-card,
.chart-card,
.table-card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 12px;
  min-width: 0;
  max-width: 100%;
  box-sizing: border-box;
}
.stat-card {
  padding: 16px 18px;
  display: grid;
  gap: 6px;
}
.stat-card span {
  color: var(--muted);
  font-size: 13px;
}
.stat-card strong {
  font-size: 25px;
  font-weight: 600;
}
.stat-card small {
  color: var(--muted);
  font-size: 12px;
}
.chart-grid {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(290px, 1fr);
  gap: 14px;
}
.chart-card {
  padding: 16px 18px 12px;
  min-width: 0;
}
.card-title {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}
.card-title h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}
.card-title span {
  color: var(--muted);
  font-size: 12px;
}
.chart {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  overflow: hidden;
}
.chart canvas {
  max-width: 100% !important;
}
.table-scroll {
  width: 100%;
  min-width: 0;
  overflow-x: auto;
}
.trend-chart {
  height: 290px;
}
.ratio-chart {
  height: 290px;
}
.chart-empty,
.table-empty {
  margin: 0;
  padding: 8px 0 14px;
  color: var(--muted);
  font-size: 13px;
  text-align: center;
}
.table-card {
  padding: 16px 18px;
}
.filter-bar {
  display: flex;
  gap: 10px;
  margin-bottom: 12px;
}
.search-input {
  width: 240px;
}
.site-cell {
  display: grid;
  gap: 2px;
}
.site-cell strong {
  font-weight: 500;
}
.site-cell span {
  color: var(--muted);
  font-size: 12px;
}
.el-table-column--default small {
  display: block;
  color: var(--muted);
}
.pagination {
  display: flex;
  justify-content: flex-end;
  padding-top: 12px;
}
@media (max-width: 1100px) {
  .chart-grid {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 800px) {
  .site-assets-panel {
    padding: 18px 14px 32px;
  }
  .page-head {
    flex-direction: column;
  }
  .stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .filter-bar {
    flex-direction: column;
  }
  .search-input {
    width: 100%;
  }
}
</style>
