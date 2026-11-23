<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';

import { toApiProblem } from '../api/client';
import {
  actOnSeedingIssue,
  executeSeedingRepair,
  previewSeedingRepair,
  getSeedingIssue,
  type SeedingIssueAction,
  type SeedingIssueDetail,
  type SeedingRepairPreview,
} from '../api/monitoring';
import {
  formatBytes,
  hoursText,
  kindLabel,
  severityLabel,
  severityTone,
  statusLabel,
  suggestionLabel,
} from '../monitoringPresentation';

const props = defineProps<{
  id: string | null;
  siteLabel: (key: string | null) => string;
  downloaderLabel: (id: string | null | undefined) => string;
}>();

const emit = defineEmits<{
  'update:id': [value: string | null];
  changed: [];
  openSites: [];
}>();

const detail = ref<SeedingIssueDetail | null>(null);
const loading = ref(false);
const acting = ref<SeedingIssueAction | null>(null);
const errorText = ref('');
const repairPreview = ref<SeedingRepairPreview | null>(null);
const repairAction = ref<string>('');
const repairDialogVisible = ref(false);
const repairSaving = ref(false);
let loadSequence = 0;

watch(
  () => props.id,
  () => {
    repairPreview.value = null;
    repairAction.value = '';
    repairDialogVisible.value = false;
  },
);

const visible = computed({
  get: () => Boolean(props.id),
  set: (value: boolean) => {
    if (!value) emit('update:id', null);
  },
});

const evidenceRows = computed(() => {
  const evidence = detail.value?.evidence ?? {};
  const site = (evidence.site ?? {}) as Record<string, unknown>;
  const downloader = (evidence.downloader ?? {}) as Record<string, unknown>;
  const tracker = (evidence.tracker ?? {}) as Record<string, unknown>;
  const filesystem = (evidence.filesystem ?? {}) as Record<string, unknown>;
  return [
    {
      key: 'site',
      title: '站点侧记录',
      state: site.result === 'SITE_ONLY' ? '站侧存在' : String(site.result ?? 'UNKNOWN'),
      text:
        site.result === 'SITE_ONLY'
          ? '站点做种列表中有该种子，下载器里没有对应任务'
          : typeof site.detail === 'string' && site.detail
            ? site.detail
            : site.result === 'FOUND'
              ? '站点侧可访问该种子'
              : '本次未获得可信结论',
    },
    {
      key: 'downloader',
      title: '下载器状态',
      state: String(downloader.state ?? 'UNKNOWN'),
      text: (downloader.detail as string | undefined) ?? '任务存在于下载器',
    },
    {
      key: 'tracker',
      title: 'Tracker 通告',
      state: String(tracker.state ?? 'UNKNOWN'),
      text:
        (tracker.message as string | undefined) ??
        (tracker.state === 'OK' ? '最近一次通告成功' : '暂无通告结果'),
    },
    {
      key: 'filesystem',
      title: '本地文件',
      state:
        filesystem.exists === false ? 'MISSING' : filesystem.exists === true ? 'OK' : 'UNKNOWN',
      text: (filesystem.path as string | undefined) ?? '路径不可见',
    },
  ];
});

async function load(): Promise<void> {
  if (!props.id) {
    detail.value = null;
    return;
  }
  const sequence = ++loadSequence;
  const issueId = props.id;
  loading.value = true;
  try {
    const loaded = await getSeedingIssue(issueId);
    if (sequence !== loadSequence || props.id !== issueId) return;
    detail.value = loaded;
    errorText.value = '';
  } catch (error) {
    if (sequence !== loadSequence || props.id !== issueId) return;
    errorText.value = toApiProblem(error).message;
  } finally {
    if (sequence === loadSequence) loading.value = false;
  }
}

async function confirmRepair(): Promise<void> {
  const preview = repairPreview.value;
  const issueId = props.id;
  if (!issueId || !preview || repairSaving.value) return;
  repairSaving.value = true;
  try {
    const result = await executeSeedingRepair(issueId, {
      action: repairAction.value,
      expected_payload_sha256: preview.payload_sha256,
    });
    repairDialogVisible.value = false;
    if (result.status === 'APPLIED') {
      ElMessage.success('已加入下载器（暂停状态），请校验后手动启动');
    } else {
      ElMessage.warning('操作已记录但回读未确认，请到下载器核对后再处理');
    }
    emit('changed');
    await load();
  } catch (error) {
    ElMessage.error(toApiProblem(error).message);
  } finally {
    repairSaving.value = false;
  }
}

async function act(action: SeedingIssueAction): Promise<void> {
  if (!props.id || acting.value) return;
  const issueId = props.id;
  if (action === 'RETRIEVE_TORRENT' || action === 'RESEED') {
    acting.value = action;
    try {
      const preview = await previewSeedingRepair(issueId, action);
      if (props.id !== issueId) return;
      repairPreview.value = preview;
      repairAction.value = action;
      repairDialogVisible.value = true;
    } catch (error) {
      ElMessage.error(toApiProblem(error).message);
    } finally {
      acting.value = null;
    }
    return;
  }
  acting.value = action;
  try {
    await actOnSeedingIssue(props.id, action);
    ElMessage.success(action === 'RECHECK' ? '已开始强制重校验' : '状态已更新');
    emit('changed');
    await load();
  } catch (error) {
    ElMessage.error(toApiProblem(error).message);
  } finally {
    acting.value = null;
  }
}

function stateTone(state: string): 'success' | 'danger' | 'info' | 'warning' {
  if (state === 'OK' || state === 'FOUND') return 'success';
  if (state === 'ERROR' || state === 'MISSING') return 'danger';
  if (state === 'UNKNOWN') return 'info';
  return 'warning';
}

watch(
  () => props.id,
  () => {
    void load();
  },
  { immediate: true },
);
</script>

<template>
  <el-drawer
    v-model="visible"
    size="min(760px, calc(100vw - 24px))"
    :with-header="false"
    class="seeding-detail-drawer"
    destroy-on-close
  >
    <div v-if="detail" class="detail">
      <header class="detail-head">
        <div>
          <h2>{{ detail.torrent_name }}</h2>
          <div class="tag-row">
            <el-tag size="small" disable-transitions>{{ kindLabel(detail.kind) }}</el-tag>
            <el-tag size="small" :type="severityTone(detail.severity)" disable-transitions>
              {{ severityLabel(detail.severity) }}
            </el-tag>
            <el-tag size="small" type="info" disable-transitions>
              {{ statusLabel(detail.status) }}
            </el-tag>
          </div>
        </div>
        <button class="close-button" type="button" @click="emit('update:id', null)">关闭</button>
      </header>

      <p v-if="errorText" class="inline-error">{{ errorText }}</p>

      <dl class="meta-grid">
        <div>
          <dt>站点</dt>
          <dd>{{ siteLabel(detail.site_key) }}</dd>
        </div>
        <div>
          <dt>下载器</dt>
          <dd>{{ downloaderLabel(detail.downloader_id) }}</dd>
        </div>
        <div>
          <dt>种子大小</dt>
          <dd>{{ formatBytes(detail.size_bytes) }}</dd>
        </div>
        <div>
          <dt>已做种</dt>
          <dd>
            {{
              detail.seeding_time_seconds
                ? `${(detail.seeding_time_seconds / 3600).toFixed(1)} 小时`
                : '未知'
            }}
          </dd>
        </div>
        <div>
          <dt>H&amp;R 剩余</dt>
          <dd>{{ hoursText(detail.hnr_remaining_hours) }}</dd>
        </div>
        <div>
          <dt>连续命中</dt>
          <dd>{{ detail.consecutive_misses }} 次</dd>
        </div>
      </dl>

      <section class="evidence-block">
        <h3>异常证据</h3>
        <ul>
          <li v-for="row in evidenceRows" :key="row.key">
            <div class="evidence-head">
              <span>{{ row.title }}</span>
              <el-tag size="small" :type="stateTone(row.state)" disable-transitions>
                {{ row.state }}
              </el-tag>
            </div>
            <p>{{ row.text }}</p>
          </li>
        </ul>
      </section>

      <section class="suggestion-block">
        <h3>修复建议</h3>
        <p class="suggestion">{{ suggestionLabel(detail.suggestion) }}</p>
        <p class="hint">修复动作不会删除本地影片文件；重建辅种会先做内容验证。</p>
        <div class="action-row">
          <el-button
            type="primary"
            :loading="acting === 'RECHECK'"
            :disabled="Boolean(acting)"
            @click="act('RECHECK')"
          >
            强制重校验
          </el-button>
          <el-button :disabled="Boolean(acting)" @click="act('RETRIEVE_TORRENT')">
            重新拉取 .torrent
          </el-button>
          <el-button :disabled="Boolean(acting)" @click="act('RESEED')">重建辅种</el-button>
        </div>
      </section>

      <footer class="detail-foot">
        <el-button text :disabled="Boolean(acting)" @click="act('IGNORE')">忽略</el-button>
        <el-button :disabled="Boolean(acting)" @click="act('ACK')">稍后处理</el-button>
        <el-button link type="primary" @click="emit('openSites')">站点配置</el-button>
      </footer>
    </div>
    <div v-else-if="loading" class="detail-loading">加载中…</div>
  </el-drawer>

  <el-dialog
    v-model="repairDialogVisible"
    title="确认修复动作"
    width="min(560px, 95vw)"
    append-to-body
  >
    <template v-if="repairPreview">
      <p class="repair-lead">
        {{ repairAction === 'RESEED' ? '重建辅种' : '重新拉取种子' }}
        将从站点取得种子文件并以<strong>暂停状态</strong>加入下载器。
      </p>
      <el-descriptions :column="1" border size="small">
        <el-descriptions-item label="种子名称">{{
          repairPreview.torrent_name
        }}</el-descriptions-item>
        <el-descriptions-item label="站点种子 ID">{{
          repairPreview.remote_torrent_id
        }}</el-descriptions-item>
        <el-descriptions-item label="目标下载器">{{
          repairPreview.downloader_name
        }}</el-descriptions-item>
        <el-descriptions-item label="保存路径">{{ repairPreview.save_path }}</el-descriptions-item>
        <el-descriptions-item label="文件数量">{{ repairPreview.file_count }}</el-descriptions-item>
        <el-descriptions-item label="内容摘要"
          >{{ repairPreview.payload_sha256.slice(0, 24) }}…</el-descriptions-item
        >
      </el-descriptions>
      <p class="repair-warning">{{ repairPreview.warning }}</p>
    </template>
    <template #footer>
      <el-button @click="repairDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="repairSaving" @click="confirmRepair">
        确认加入下载器
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.repair-lead {
  margin: 0 0 12px;
  font-size: 13px;
  line-height: 1.7;
}
.repair-warning {
  margin: 12px 0 0;
  font-size: 12px;
  color: var(--el-color-warning);
  line-height: 1.7;
}
.detail {
  display: grid;
  gap: 18px;
  padding: 4px 4px 24px;
}
/* 抽屉正文只做纵向滚动：长种子名/路径一律在容器内换行，不出现横向滚动条。 */
.seeding-detail-drawer :deep(.el-drawer__body) {
  overflow-x: hidden;
}
.detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.detail-head > div {
  min-width: 0;
}
.detail-head h2 {
  margin: 0 0 8px;
  font-size: 17px;
  font-weight: 500;
  line-height: 1.4;
  overflow-wrap: anywhere;
}
.tag-row {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.close-button {
  border: 0;
  background: transparent;
  color: var(--muted);
  font-size: 13px;
  cursor: pointer;
}
.inline-error {
  margin: 0;
  padding: 8px 12px;
  border-radius: 8px;
  background: #fdecec;
  color: #8a2222;
  font-size: 12px;
}
.meta-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px 20px;
  margin: 0;
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: 12px;
}
.meta-grid dt {
  font-size: 11px;
  color: var(--muted);
}
.meta-grid dd {
  margin: 2px 0 0;
  font-size: 13px;
  overflow-wrap: anywhere;
}
.evidence-block h3,
.suggestion-block h3 {
  margin: 0 0 10px;
  font-size: 13px;
  font-weight: 500;
  color: var(--muted);
}
.evidence-block ul {
  display: grid;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.evidence-block li {
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
}
.evidence-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  font-size: 13px;
}
.evidence-head > span:first-child {
  min-width: 0;
  overflow-wrap: anywhere;
}
.evidence-head :deep(.el-tag) {
  flex: none;
}
.evidence-block p {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--muted);
  line-height: 1.6;
  word-break: break-all;
}
.suggestion-block {
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--blue-soft);
}
.suggestion {
  margin: 0 0 4px;
  font-size: 14px;
  font-weight: 500;
}
.hint {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--muted);
}
.action-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.detail-foot {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}
.detail-loading {
  padding: 40px 0;
  text-align: center;
  color: var(--muted);
  font-size: 13px;
}
@media (max-width: 640px) {
  .meta-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .detail-foot {
    flex-wrap: wrap;
    justify-content: flex-start;
  }
}
</style>
