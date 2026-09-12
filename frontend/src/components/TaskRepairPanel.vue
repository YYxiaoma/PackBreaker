<script setup lang="ts">
import { computed, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { RefreshCw, ShieldCheck, Wrench } from '@lucide/vue';

import { ApiProblem } from '../api/client';
import {
  executeTaskUnitRepair,
  getTaskUnitRepairPlan,
  type RepairExecuteAction,
  type RepairMode,
  type RepairPlan,
} from '../api/tasks';

const unitId = ref('');
const mode = ref<RepairMode>('AUTO_PIECE');
const plan = ref<RepairPlan | null>(null);
const execution = ref<RepairExecuteAction | null>(null);
const loading = ref(false);
const executing = ref(false);
const idempotencyKey = ref('');

const canExecute = computed(
  () =>
    mode.value === 'AUTO_PIECE' &&
    plan.value?.ready === true &&
    plan.value.blocked_reasons.length === 0 &&
    execution.value === null,
);

const modeHelp: Record<RepairMode, string> = {
  AUTO_PIECE: '后端重新证明全部安全事实后，允许进入 inode 隔离与受控下载修复。',
  FILE_ONLY: '只读评估文件级修复是否安全；不会调用 repair execute。',
  GUIDED: '只读输出影响范围和人工处理建议；不会调用 repair execute。',
};

const blockLabels: Record<string, string> = {
  NO_REPAIR_NEEDED: '当前没有需要修复的内容',
  VERIFICATION_BLOCKED: '验证证据已阻断',
  TARGET_EVIDENCE_MISSING: '目标文件证据缺失',
  TARGET_MISSING: '目标文件缺失',
  TARGET_LENGTH_MISMATCH: '目标文件长度不一致',
  TARGET_SOURCE_IDENTITY_UNKNOWN: '无法证明目标与源 inode 关系',
  DOWNLOADER_NOT_PAUSED: '目标下载器未停止写入',
  INSUFFICIENT_SPACE: '隔离或补齐所需空间不足',
  FILE_ONLY_CROSS_FILE_PIECE: '文件级模式遇到跨文件 piece',
  FILE_ONLY_REQUIRES_ISOLATION: '文件级模式需要 inode 隔离',
};

async function loadPlan(): Promise<void> {
  const id = unitId.value.trim();
  if (!id) {
    ElMessage.warning('请输入真实后端 task unit ID');
    return;
  }
  loading.value = true;
  try {
    plan.value = await getTaskUnitRepairPlan(id, mode.value);
    execution.value = null;
    idempotencyKey.value = newRepairKey();
    ElMessage.success('已重新读取服务端可信 repair plan');
  } catch (error) {
    plan.value = null;
    execution.value = null;
    showError(error);
  } finally {
    loading.value = false;
  }
}

async function executeRepair(): Promise<void> {
  if (!canExecute.value || !plan.value) return;
  const id = unitId.value.trim();
  if (!idempotencyKey.value) idempotencyKey.value = newRepairKey();
  try {
    await ElMessageBox.confirm(
      '浏览器不会提交 inode、hash、ownership、journal 或暂停状态。服务端会重新生成 AUTO_PIECE 计划，并在每个副作用前重新证明下载器与文件系统证据。确认开始受控修复？',
      '确认执行真实安全修复',
      {
        confirmButtonText: '确认并由后端重新验证',
        cancelButtonText: '返回检查',
        type: 'warning',
      },
    );
  } catch {
    return;
  }

  executing.value = true;
  try {
    execution.value = await executeTaskUnitRepair(id, idempotencyKey.value);
    ElMessage.success('repair execute 已受理，后续由任务驱动器推进下载补齐与完整重校验');
  } catch (error) {
    // 不更换幂等键：网络/响应丢失后重试必须命中同一 receipt。
    showError(error);
  } finally {
    executing.value = false;
  }
}

function resetPlan(): void {
  plan.value = null;
  execution.value = null;
  idempotencyKey.value = '';
}

function newRepairKey(): string {
  return `repair-${globalThis.crypto.randomUUID()}`;
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

function showError(error: unknown): void {
  if (error instanceof ApiProblem) {
    ElMessage.error(`${error.code}：${error.message}`);
    return;
  }
  ElMessage.error(error instanceof Error ? error.message : '安全修复请求失败');
}
</script>

<template>
  <section class="repair-live-panel">
    <div class="repair-live-title">
      <div>
        <span class="eyebrow">真实后端安全修复区</span>
        <h4>可信 repair plan / execute</h4>
      </div>
      <ShieldCheck :size="22" />
    </div>

    <el-alert
      title="repair plan 永远不是写授权"
      description="GET 只返回脱敏影响范围；POST execute 只提交 unit + 动作 + Idempotency-Key。暂停状态、inode、hash、ownership、journal 与空间证据全部由后端重新读取和证明。"
      type="info"
      :closable="false"
      show-icon
    />

    <div class="repair-live-form">
      <el-input v-model="unitId" placeholder="真实 task unit ID（UUID）" @input="resetPlan" />
      <el-select v-model="mode" @change="resetPlan">
        <el-option label="AUTO_PIECE · 自动 piece 修复" value="AUTO_PIECE" />
        <el-option label="FILE_ONLY · 文件级只读评估" value="FILE_ONLY" />
        <el-option label="GUIDED · 人工引导" value="GUIDED" />
      </el-select>
      <el-button :loading="loading" @click="loadPlan"
        ><RefreshCw :size="15" />读取真实计划</el-button
      >
    </div>
    <p class="muted">{{ modeHelp[mode] }}</p>

    <template v-if="plan">
      <el-alert
        :title="plan.ready ? '当前 repair plan 已通过只读安全门' : '当前 repair plan 已阻断'"
        :type="plan.ready ? 'success' : 'warning'"
        :closable="false"
        show-icon
      />
      <el-descriptions :column="2" border class="repair-live-summary">
        <el-descriptions-item label="下载器">{{ plan.downloader_kind }}</el-descriptions-item>
        <el-descriptions-item label="真实暂停状态">
          {{ plan.downloader_paused ? '已停止写入' : '未停止' }}
        </el-descriptions-item>
        <el-descriptions-item label="受影响 piece">
          {{ plan.affected_pieces.length }}（跨文件 {{ plan.cross_file_pieces.length }}）
        </el-descriptions-item>
        <el-descriptions-item label="受影响文件">{{
          plan.affected_files.length
        }}</el-descriptions-item>
        <el-descriptions-item label="inode 隔离预算">
          {{ formatBytes(plan.isolation_bytes_required) }}
        </el-descriptions-item>
        <el-descriptions-item label="预计补齐上限">
          {{ formatBytes(plan.estimated_download_bytes_upper_bound) }}
        </el-descriptions-item>
        <el-descriptions-item label="需要空闲空间">
          {{ formatBytes(plan.required_free_bytes) }}
        </el-descriptions-item>
        <el-descriptions-item label="当前可用空间">
          {{ formatBytes(plan.available_bytes) }}
        </el-descriptions-item>
      </el-descriptions>

      <div v-if="plan.blocked_reasons.length" class="repair-blockers">
        <b>阻断原因</b>
        <el-tag v-for="reason in plan.blocked_reasons" :key="reason" type="danger">
          {{ blockLabels[reason] ?? reason }}
        </el-tag>
      </div>

      <div v-if="plan.affected_files.length" class="repair-file-list">
        <article v-for="file in plan.affected_files" :key="file.torrent_path">
          <div>
            <b>{{ file.torrent_path }}</b>
            <small>
              {{ formatBytes(file.length) }} · {{ file.mapping_state }} ·
              {{
                file.whole_file_fetch ? '完整文件补齐' : `${file.affected_pieces.length} 个 piece`
              }}
            </small>
          </div>
          <el-tag v-if="file.isolation_required" type="warning">需先隔离 inode</el-tag>
          <el-tag v-else type="success">无需再次隔离</el-tag>
        </article>
      </div>

      <div class="repair-execute-row">
        <el-button
          type="danger"
          :disabled="!canExecute"
          :loading="executing"
          @click="executeRepair"
        >
          <Wrench :size="15" />执行受控 AUTO_PIECE 修复
        </el-button>
        <small v-if="mode !== 'AUTO_PIECE'">该模式只读，不提供执行按钮。</small>
        <small v-else-if="!plan.ready">安全门未通过，禁止执行。</small>
        <small v-else>执行时后端仍会重新验证，不信任这份旧 plan。</small>
      </div>
    </template>

    <el-alert
      v-if="execution"
      :title="`repair execute 已受理：${execution.status}`"
      :description="`task ${execution.task_id} · version ${execution.task_version} · receipt ${execution.receipt_id}`"
      type="success"
      :closable="false"
      show-icon
    />
  </section>
</template>

<style scoped>
.repair-live-panel {
  display: grid;
  gap: 14px;
  margin-top: 20px;
  padding: 18px;
  border: 1px solid var(--el-border-color);
  border-radius: 12px;
}

.repair-live-title,
.repair-execute-row,
.repair-file-list article {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.repair-live-title h4 {
  margin: 4px 0 0;
}

.repair-live-form {
  display: grid;
  grid-template-columns: minmax(240px, 1fr) minmax(220px, 0.6fr) auto;
  gap: 10px;
}

.repair-live-summary {
  width: 100%;
}

.repair-blockers {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.repair-file-list {
  display: grid;
  gap: 8px;
}

.repair-file-list article {
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}

.repair-file-list small {
  display: block;
  margin-top: 4px;
  color: var(--el-text-color-secondary);
}

@media (max-width: 720px) {
  .repair-live-form {
    grid-template-columns: 1fr;
  }

  .repair-live-title,
  .repair-execute-row,
  .repair-file-list article {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
