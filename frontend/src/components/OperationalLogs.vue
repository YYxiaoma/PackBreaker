<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue';
import { Download, RefreshCw, Search, ShieldCheck } from '@lucide/vue';
import { ElMessage } from 'element-plus';
import { toApiProblem } from '../api/client';
import {
  exportSystemLogs,
  listSystemLogs,
  type OperationalLogEntry,
  type OperationalLogLevel,
  type OperationalLogList,
  type OperationalLogQuery,
} from '../api/system';

const loading = ref(false);
const exportLoading = ref(false);
const result = ref<OperationalLogList | null>(null);
const filters = reactive<{
  q: string;
  level: '' | OperationalLogLevel;
  window_minutes: number;
}>({
  q: '',
  level: '',
  window_minutes: 60,
});

function query(limit: number): OperationalLogQuery {
  return {
    window_minutes: filters.window_minutes,
    limit,
    ...(filters.level ? { level: filters.level } : {}),
    ...(filters.q.trim() ? { q: filters.q.trim() } : {}),
  };
}

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    result.value = await listSystemLogs(query(200));
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    loading.value = false;
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

async function exportLogs(): Promise<void> {
  exportLoading.value = true;
  try {
    const artifact = await exportSystemLogs(query(2000));
    downloadArtifact(artifact.blob, artifact.filename);
    ElMessage.success('已导出当前受控窗口的脱敏日志；导出硬上限 2000 条');
  } catch (caught) {
    ElMessage.error(toApiProblem(caught).message);
  } finally {
    exportLoading.value = false;
  }
}

function tagType(level: OperationalLogLevel): 'info' | 'warning' | 'danger' | 'primary' {
  if (level === 'ERROR' || level === 'CRITICAL') return 'danger';
  if (level === 'WARNING') return 'warning';
  if (level === 'DEBUG') return 'info';
  return 'primary';
}

function formatTimestamp(value: string): string {
  return new Date(value).toLocaleString();
}

function formatFields(entry: OperationalLogEntry): string {
  const fields = Object.entries(entry.fields);
  if (!fields.length) return '';
  return fields
    .slice(0, 6)
    .map(([key, value]) => `${key}=${typeof value === 'string' ? value : JSON.stringify(value)}`)
    .join(' · ');
}

function formatBytes(value: number): string {
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${Math.round(value / 1024)} KiB`;
}

onMounted(() => void refresh());
</script>

<template>
  <div class="operational-logs">
    <div class="section-heading">
      <div>
        <h2>运行日志 <small>持久轮转 · 默认脱敏 · 查询窗口受控</small></h2>
        <p v-if="result" class="log-policy">
          <ShieldCheck :size="14" /> `/config/logs` 近似容量上限
          {{ formatBytes(result.approximate_capacity_bytes) }} ·
          {{ result.backup_count }} 个轮转备份 · 当前返回 {{ result.count }} 条
          <template v-if="result.truncated"> · 已按 {{ result.limit }} 条截断</template>
        </p>
      </div>
      <el-button :loading="exportLoading" @click="exportLogs">
        <Download :size="15" />导出当前窗口
      </el-button>
    </div>

    <el-alert
      title="这里只读取 PackBreaker 自身的有界 JSONL 日志，不读取 Docker daemon 日志。写入和读取都会经过脱敏；URL 只保留 origin，异常只记录类型。"
      type="info"
      :closable="false"
      show-icon
    />

    <div class="filters log-controls">
      <el-input
        v-model="filters.q"
        maxlength="128"
        clearable
        placeholder="搜索 message、logger、trace_id 或安全字段"
        @keyup.enter="refresh"
      >
        <template #prefix><Search :size="16" /></template>
      </el-input>
      <el-select v-model="filters.level" placeholder="全部级别" clearable @change="refresh">
        <el-option
          v-for="level in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']"
          :key="level"
          :value="level"
        />
      </el-select>
      <el-select v-model="filters.window_minutes" @change="refresh">
        <el-option label="最近 15 分钟" :value="15" />
        <el-option label="最近 1 小时" :value="60" />
        <el-option label="最近 6 小时" :value="360" />
        <el-option label="最近 24 小时" :value="1440" />
        <el-option label="最近 7 天（上限）" :value="10080" />
      </el-select>
      <el-button :loading="loading" @click="refresh"><RefreshCw :size="15" />刷新</el-button>
    </div>

    <section class="panel log-panel" v-loading="loading">
      <article
        v-for="entry in result?.items ?? []"
        :key="`${entry.timestamp}-${entry.logger}-${entry.message}`"
        class="log-line real-log-line"
      >
        <time>{{ formatTimestamp(entry.timestamp) }}</time>
        <el-tag :type="tagType(entry.level)">{{ entry.level }}</el-tag>
        <div>
          <b>{{ entry.message }}</b>
          <small
            >{{ entry.logger
            }}<template v-if="entry.exception"> · exception={{ entry.exception }}</template></small
          >
          <small v-if="formatFields(entry)" class="log-fields">{{ formatFields(entry) }}</small>
        </div>
      </article>
      <el-empty
        v-if="!loading && !(result?.items.length ?? 0)"
        description="当前受控窗口内没有匹配日志"
      />
    </section>
  </div>
</template>

<style scoped>
.operational-logs {
  display: grid;
  gap: 14px;
}
.section-heading {
  margin-bottom: 0;
}
.section-heading > div:first-child {
  display: block;
}
.log-policy {
  margin: 7px 0 0;
  color: var(--muted);
  font-size: 11px;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.log-controls {
  margin: 0;
  padding-top: 2px;
}
.log-controls > .el-input {
  width: min(430px, 100%);
}
.log-controls > .el-select {
  width: 170px;
}
.log-panel {
  padding: 0 20px;
}
.real-log-line {
  display: grid;
  grid-template-columns: 170px 80px minmax(0, 1fr);
  gap: 14px;
  align-items: start;
  padding: 15px 0;
  border-bottom: 1px solid var(--line);
}
.real-log-line:last-child {
  border-bottom: 0;
}
.real-log-line time {
  color: var(--muted);
  font-family: Consolas, monospace;
  font-size: 11px;
}
.real-log-line b,
.real-log-line small {
  display: block;
  overflow-wrap: anywhere;
}
.real-log-line b {
  font-size: 12px;
  line-height: 1.6;
}
.real-log-line small {
  color: var(--muted);
  margin-top: 5px;
  font-size: 10px;
}
.log-fields {
  font-family: Consolas, monospace;
}
@media (max-width: 800px) {
  .section-heading {
    align-items: flex-start;
    gap: 12px;
    flex-direction: column;
  }
  .log-controls > .el-select,
  .log-controls > .el-button {
    flex: 1;
    min-width: 130px;
  }
  .real-log-line {
    grid-template-columns: 1fr auto;
  }
  .real-log-line > div {
    grid-column: 1 / -1;
  }
}
</style>
