<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { AlertTriangle, CheckCircle2, RefreshCw, Search, ShieldCheck } from '@lucide/vue';

import { ApiProblem } from '../api/client';
import {
  analyzeTask,
  getTaskPreflight,
  getTaskPreflightCurrent,
  listTaskCandidates,
  listTaskUnits,
  type Preflight,
  type TaskCandidate,
  type TaskUnit,
} from '../api/tasks';

const props = defineProps<{ suggestedTaskId?: string }>();

const taskId = ref(isBackendTaskId(props.suggestedTaskId) ? props.suggestedTaskId! : '');
const sourceRoot = ref('.');
const units = ref<TaskUnit[]>([]);
const candidates = ref<TaskCandidate[]>([]);
const preflight = ref<Preflight | null>(null);
const loading = ref(false);
const analyzing = ref(false);
const loadedTaskId = ref('');

watch(
  () => props.suggestedTaskId,
  (value) => {
    if (isBackendTaskId(value) && !taskId.value) taskId.value = value!;
  },
);

const staleLabel = computed(
  () => preflight.value?.stale_reasons.map((reason) => staleReasonLabel(reason)).join('；') ?? '',
);

async function load(): Promise<void> {
  const id = taskId.value.trim();
  if (!id) {
    ElMessage.warning('请输入真实后端 task ID');
    return;
  }
  loading.value = true;
  try {
    const [loadedUnits, loadedCandidates] = await Promise.all([
      listTaskUnits(id),
      listTaskCandidates(id),
    ]);
    units.value = loadedUnits;
    candidates.value = loadedCandidates;
    try {
      preflight.value = await getTaskPreflight(id);
    } catch (error) {
      if (error instanceof ApiProblem && error.code === 'PREFLIGHT_NOT_FOUND') {
        preflight.value = null;
      } else {
        throw error;
      }
    }
    loadedTaskId.value = id;
  } catch (error) {
    showError(error);
  } finally {
    loading.value = false;
  }
}

async function analyze(): Promise<void> {
  const id = taskId.value.trim();
  const root = sourceRoot.value.trim();
  if (!id || !root) {
    ElMessage.warning('task ID 与 source_root 都不能为空');
    return;
  }
  analyzing.value = true;
  try {
    preflight.value = await analyzeTask(id, root);
    loadedTaskId.value = id;
    [units.value, candidates.value] = await Promise.all([
      listTaskUnits(id),
      listTaskCandidates(id),
    ]);
    ElMessage.success('只读分析完成，已生成不可变 preflight snapshot');
  } catch (error) {
    showError(error);
  } finally {
    analyzing.value = false;
  }
}

async function refreshCurrent(): Promise<void> {
  if (!loadedTaskId.value || preflight.value === null) return;
  try {
    const current = await getTaskPreflightCurrent(loadedTaskId.value);
    preflight.value = {
      ...preflight.value,
      current: current.current,
      stale_reasons: current.stale_reasons,
      snapshot_digest: current.snapshot_digest,
    };
  } catch (error) {
    showError(error);
  }
}

function showError(error: unknown): void {
  if (error instanceof ApiProblem) {
    ElMessage.error(`${error.code}：${error.message}`);
    return;
  }
  ElMessage.error(error instanceof Error ? error.message : '任务分析请求失败');
}

function isBackendTaskId(value: string | undefined): boolean {
  return Boolean(value && /^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(value));
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return '—';
  const labels = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < labels.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size >= 10 || index === 0 ? size.toFixed(0) : size.toFixed(1)} ${labels[index]}`;
}

function verificationType(level: string | null): 'success' | 'warning' | 'danger' | 'info' {
  if (level === 'FULL_VERIFIED') return 'success';
  if (level === 'CLIENT_CHECK_REQUIRED') return 'warning';
  if (level === 'BLOCKED') return 'danger';
  return 'info';
}

function staleReasonLabel(reason: string): string {
  const labels: Record<string, string> = {
    TASK_VERSION_CHANGED: '任务版本已变化',
    SOURCE_CHANGED: '源文件清单或快照已变化',
    SOURCE_UNAVAILABLE: '源目录当前不可用',
    SITE_CONFIG_CHANGED: '启用站点或站点版本已变化',
    UNIT_RECORD_MISSING: '处理单元证据缺失',
    PREFLIGHT_EVIDENCE_INVALID: '预演证据结构无效',
  };
  return labels[reason] ?? reason;
}
</script>

<template>
  <section class="live-analysis-panel">
    <el-alert
      title="真实后端分析区"
      description="这里的数据来自 /api/v1/tasks/*。PB-xxx 演示任务不会自动映射为真实任务；source_root 必须是 /data 下的相对目录。Analyze 只执行搜索、取种元数据、解析、映射和 piece 验证，不调用下载器写接口。"
      type="info"
      :closable="false"
      show-icon
    />
    <div class="live-analysis-form">
      <el-input v-model="taskId" placeholder="真实 task ID（UUID）" aria-label="真实 task ID" />
      <el-input
        v-model="sourceRoot"
        placeholder="source_root，例如 movie/Season.01 或 ."
        aria-label="源目录相对路径"
      />
      <el-button :loading="loading" @click="load"><Search :size="15" />加载证据</el-button>
      <el-button type="primary" :loading="analyzing" @click="analyze">
        <ShieldCheck :size="15" />Analyze
      </el-button>
    </div>

    <template v-if="preflight">
      <el-alert
        :title="preflight.current ? '当前 Preflight 有效' : '当前 Preflight 已失效'"
        :description="
          preflight.current
            ? `snapshot ${preflight.snapshot_digest.slice(0, 16)}… 与当前任务、源文件和站点版本一致。`
            : staleLabel
        "
        :type="preflight.current ? 'success' : 'warning'"
        :closable="false"
        show-icon
      />
      <div class="live-preflight-meta">
        <code>{{ preflight.snapshot_digest }}</code>
        <span>{{ new Date(preflight.created_at).toLocaleString() }}</span>
        <el-button link type="primary" @click="refreshCurrent">
          <RefreshCw :size="14" />刷新当前性
        </el-button>
      </div>
    </template>
    <el-empty v-else-if="loadedTaskId" description="该任务尚未生成 preflight，可执行 Analyze" />

    <h3 class="detail-section-title">
      真实处理单元 <span>{{ units.length }} 项</span>
    </h3>
    <div v-if="units.length" class="live-unit-list">
      <article v-for="unit in units" :key="unit.id" class="live-evidence-card">
        <div>
          <b>{{ unit.source_relative_path }}</b>
          <small
            >{{ unit.kind }} · {{ formatBytes(unit.length) }} · root={{ unit.source_root }}</small
          >
        </div>
        <code>{{ unit.normalized_unit_key.slice(0, 16) }}…</code>
      </article>
    </div>
    <el-empty v-else description="尚无持久化处理单元" />

    <h3 class="detail-section-title">
      真实候选证据 <span>{{ candidates.length }} 项</span>
    </h3>
    <div v-if="candidates.length" class="live-candidate-list">
      <article v-for="item in candidates" :key="item.id" class="candidate-card">
        <div class="candidate-top">
          <span class="mini-logo">{{ item.site_id.slice(0, 2).toUpperCase() }}</span>
          <b>{{ item.display_name }}</b>
          <strong>{{ item.score }}<small> 分</small></strong>
        </div>
        <p>
          {{ item.site_id }} · torrent {{ item.torrent_id }} ·
          {{ item.selected_for_verification ? '已进入深度验证' : '仅排序证据' }}
        </p>
        <div class="candidate-bottom">
          <el-tag :type="verificationType(item.verification_level)">
            {{ item.verification_level ?? (item.rejected ? 'HARD_REJECTED' : 'NOT_VERIFIED') }}
          </el-tag>
          <span v-if="item.error_code" class="red"
            ><AlertTriangle :size="14" />{{ item.error_code }}</span
          >
          <span v-else class="green"><CheckCircle2 :size="14" />无候选错误</span>
        </div>
        <code v-if="item.metainfo_digest">metainfo {{ item.metainfo_digest }}</code>
      </article>
    </div>
    <el-empty v-else description="尚无候选证据" />
  </section>
</template>
