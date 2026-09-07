<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import {
  ShieldCheck,
  Check,
  FileVideo,
  ArrowRight,
  Download,
  AlertTriangle,
  Play,
  RotateCcw,
  GitBranch,
} from '@lucide/vue';
import {
  approveTask,
  finishVerification,
  canApprove,
  stateNames,
  levelNames,
  type Task,
  type Level,
} from '../demo';
const props = defineProps<{ task: Task }>();
const emit = defineEmits<{ export: [unknown, string] }>();
const tab = ref('计划'),
  candidate = ref('primary'),
  mapped = ref(''),
  revision = ref(1),
  repair = ref('人工引导'),
  approved = ref(false);
watch(
  () => props.task.id,
  () => {
    tab.value = '计划';
    candidate.value = 'primary';
    mapped.value = '';
    revision.value = 1;
    approved.value = false;
  },
);
const blocked = computed(() => props.task.level === 'BLOCKED');
const repairScenario = computed(() => props.task.name.includes('森林'));
const files = computed(() =>
  Array.from({ length: Math.min(props.task.units, 4) }, (_, i) => ({
    source: `/data/source/${props.task.kind === '剧集拆包' ? 'Season.01/' : ''}${props.task.kind === '剧集拆包' ? `S01E0${i + 1}` : `Movie.0${i + 1}`}.1080p.mkv`,
    target: `/data/seeding/${props.task.site}/${props.task.kind === '剧集拆包' ? `S01E0${i + 1}` : `Movie.0${i + 1}`}.mkv`,
    size: i === 0 ? '10.8 GB' : '9.6 GB',
    action: blocked.value && i === 0 ? '等待解决冲突' : '创建硬链接',
  })),
);
const candidates = computed(() => [
  {
    key: 'primary',
    name: `${props.task.name} · 1080p BluRay x265`,
    site: props.task.site,
    score: 98,
    level: (blocked.value ? 'BLOCKED' : props.task.level) as Level,
    reason: blocked.value ? '文件映射或设备检查未通过' : '片名 / 年份 / 季集 / 文件长度匹配',
  },
  {
    key: 'alternative',
    name: `${props.task.name} · 1080p WEB-DL`,
    site: props.task.site === 'M-Team' ? 'HDTime' : 'M-Team',
    score: 87,
    level: 'CLIENT_CHECK_REQUIRED' as Level,
    reason: '版本不同，尚未完成候选 piece 校验',
  },
  {
    key: 'blocked',
    name: `${props.task.name} · 2160p Remux`,
    site: 'HDTime',
    score: 63,
    level: 'BLOCKED' as Level,
    reason: '分辨率、文件长度与来源不兼容',
  },
]);
function choose(key: string) {
  if (props.task.state !== 'AWAITING_CONFIRMATION') return;
  candidate.value = key;
  revision.value++;
  props.task.level = key === 'blocked' ? 'BLOCKED' : 'CLIENT_CHECK_REQUIRED';
  props.task.error = key === 'blocked' ? 'FILE_LENGTH_MISMATCH' : undefined;
  props.task.events.push('演示候选已变更，旧预演失效；需重新校验');
  ElMessage.info('旧预演已失效，请重新模拟验证');
  tab.value = '文件映射';
}
function reverify() {
  if (props.task.error) {
    ElMessage.warning('请先解决阻断原因');
    return;
  }
  props.task.level = candidate.value === 'alternative' ? 'CLIENT_CHECK_REQUIRED' : 'FULL_VERIFIED';
  props.task.progress = props.task.level === 'FULL_VERIFIED' ? 100 : 0;
  revision.value++;
  props.task.events.push('重新生成演示预演快照');
  ElMessage.success('已生成新的演示验证结果');
  tab.value = '计划';
}
function mapFile() {
  if (!mapped.value) {
    ElMessage.warning('请选择与候选对应的源文件');
    return;
  }
  props.task.error = undefined;
  props.task.level = 'CLIENT_CHECK_REQUIRED';
  revision.value++;
  props.task.events.push('人工选择源文件；原验证证据失效');
  ElMessage.success('映射已更新，请重新验证');
  mapped.value = '';
  tab.value = '计划';
}
async function approve() {
  if (!canApprove(props.task)) return;
  try {
    await ElMessageBox.confirm(
      `预演 ${props.task.id}-v${revision.value}：模拟创建 ${files.value.length} 个硬链接，并暂停添加到 ${props.task.client}；随后执行完整客户端校验。${repairScenario.value ? '缺失附件预计补齐 128 MB，修复前需隔离受影响目标。' : ''}`,
      '确认执行演示计划',
      { confirmButtonText: '批准模拟执行', cancelButtonText: '返回检查', type: 'info' },
    );
    if (approveTask(props.task)) {
      approved.value = true;
      ElMessage.success('已模拟添加，请在面板中模拟客户端校验完成');
    }
  } catch {}
}
async function runRepair() {
  try {
    await ElMessageBox.confirm(
      `将演示「${repair.value}」。受影响文件：S01E01.mkv、poster.jpg；piece #2047–2050。需先复制并原子替换目标媒体为独立 inode，空间预算 10.8 GB，预计补齐 128 MB。`,
      '确认安全修复演示',
      { confirmButtonText: '演示隔离与修复', cancelButtonText: '取消', type: 'warning' },
    );
    props.task.events.push(`演示修复模式：${repair.value}；已模拟独立 inode 隔离，未操作真实文件`);
    ElMessage.success('隔离与修复计划已加入演示时间线');
  } catch {}
}
</script>
<template>
  <div class="detail-head">
    <div>
      <span class="eyebrow">{{ task.id }} · {{ task.kind }}</span>
      <p>
        {{ task.site }} <ArrowRight :size="13" /> {{ task.client }} <span> · {{ task.size }}</span>
      </p>
    </div>
    <el-tag :type="blocked ? 'danger' : task.state === 'SEEDING' ? 'success' : 'primary'">{{
      stateNames[task.state]
    }}</el-tag>
  </div>
  <el-alert
    :title="
      blocked
        ? '执行已阻断：' +
          (task.error === 'FILE_MAPPING_AMBIGUOUS'
            ? '两个源文件对应同一候选路径'
            : task.error === 'CROSS_DEVICE_LINK'
              ? '源与目标不在同一文件系统'
              : '候选文件长度不兼容')
        : task.level === 'FULL_VERIFIED'
          ? '演示结果：全部候选 piece 校验通过'
          : '演示结果：映射可信，仍需下载器完整校验'
    "
    :type="blocked ? 'error' : task.level === 'FULL_VERIFIED' ? 'success' : 'warning'"
    :closable="false"
    show-icon
  />
  <div class="detail-tabs">
    <el-tabs v-model="tab"
      ><el-tab-pane
        v-for="s in ['计划', '处理单元', '候选证据', '文件映射', '时间线与资源', '安全修复']"
        :key="s"
        :name="s"
        :label="s"
    /></el-tabs>
  </div>
  <template v-if="tab === '计划'">
    <div class="detail-metrics">
      <div>
        <small>验证等级</small><b :class="blocked ? 'red' : 'green'">{{ levelNames[task.level] }}</b
        ><code>{{ task.level }}</code>
      </div>
      <div>
        <small>预计补齐量</small><b>{{ repairScenario ? '128 MB' : '0 B' }}</b
        ><span>演示估算</span>
      </div>
      <div>
        <small>执行快照</small><b>v{{ revision }}</b
        ><span>候选变更后需重新预演</span>
      </div>
    </div>
    <h3 class="detail-section-title">执行前检查 <span>演示结果</span></h3>
    <div class="check-grid">
      <div
        v-for="s in [
          '源任务已完成且通过稳定期',
          '目标路径未越过允许根目录',
          '源数据始终保持只读',
          '按操作日志登记资源所有权',
        ]"
        :key="s"
      >
        <Check :size="15" />{{ s }}
      </div>
      <div>
        <component :is="blocked ? AlertTriangle : Check" :size="15" />{{
          blocked ? '文件映射 / 设备检查未通过' : '源与目标位于同一设备'
        }}
      </div>
      <div><Check :size="15" />可用空间 420 GB</div>
    </div>
    <h3 class="detail-section-title">计划动作</h3>
    <div
      class="plan-step"
      v-for="(s, i) in [
        '检查源文件快照、路径和空间',
        '登记并创建 ' + files.length + ' 个目标硬链接',
        '暂停添加到 ' + task.client,
        '执行客户端完整校验，100% 后确认做种',
      ]"
      :key="s"
    >
      <span>{{ i + 1 }}</span>
      <div>
        <b>{{ s }}</b
        ><small>{{
          i === 2
            ? 'qB 跳过校验默认关闭；Transmission 始终校验'
            : i === 0
              ? '源数据变化后本次预演作废'
              : i === 1
                ? '目标：/data/seeding/' + task.site
                : '模拟反馈不会启动真实下载任务'
        }}</small>
      </div>
    </div>
    <div class="safety-note">
      <ShieldCheck :size="19" /><span
        >评分只用于候选排序。跳过校验必须同时满足 FULL_VERIFIED、qB 和显式启用。</span
      >
    </div>
    <div v-if="blocked" class="detail-block">
      <b>{{ task.error }}</b>
      <p>
        {{
          task.error === 'FILE_MAPPING_AMBIGUOUS'
            ? '请在「文件映射」中指定正确源文件，然后重新验证。'
            : task.error === 'CROSS_DEVICE_LINK'
              ? '需将源与目标挂载共同父目录，重新运行下载器路径诊断。不能静默复制整个媒体文件。'
              : '请更换与源数据长度兼容的候选。'
        }}
      </p>
      <el-button @click="tab = task.error === 'FILE_MAPPING_AMBIGUOUS' ? '文件映射' : '候选证据'"
        >查看阻断证据</el-button
      >
    </div>
  </template>
  <template v-else-if="tab === '处理单元'"
    ><p class="muted">
      共识别 {{ task.units }} 个{{ task.kind === '剧集拆包' ? '剧集' : '影片' }}单元；显示前
      {{ Math.min(task.units, 8) }} 个合成样例。
    </p>
    <div class="unit-row" v-for="n in Math.min(task.units, 8)" :key="n">
      <FileVideo :size="22" />
      <div>
        <b>{{
          task.kind === '剧集拆包'
            ? `S01E${String(n).padStart(2, '0')}`
            : `影片 ${String(n).padStart(2, '0')}`
        }}</b
        ><small>1080p · BluRay · x265 · {{ task.site }}</small>
      </div>
      <el-tag>{{ n === 1 ? levelNames[task.level] : '待单元审核' }}</el-tag
      ><el-button link type="primary" @click="tab = '候选证据'">查看示例候选</el-button>
    </div>
    <el-alert
      title="当前使用代表性单元演示审核流程；逐单元独立执行将在后端阶段实现。"
      type="info"
      :closable="false"
  /></template>
  <template v-else-if="tab === '候选证据'"
    ><p class="muted">候选排序参考名称、媒体信息与结构；高分不代表内容字节相同。</p>
    <article
      v-for="c in candidates"
      :key="c.key"
      :class="['candidate-card', { picked: candidate === c.key }]"
    >
      <div class="candidate-top">
        <span class="mini-logo">{{ c.site === 'M-Team' ? 'MT' : 'HD' }}</span
        ><b>{{ c.name }}</b
        ><strong>{{ c.score }}<small> 分</small></strong>
      </div>
      <p>{{ c.reason }}</p>
      <div class="candidate-bottom">
        <el-tag
          :type="
            c.level === 'BLOCKED' ? 'danger' : c.level === 'FULL_VERIFIED' ? 'success' : 'warning'
          "
          >{{ levelNames[c.level] }}</el-tag
        ><el-button
          size="small"
          :disabled="task.state !== 'AWAITING_CONFIRMATION' || candidate === c.key"
          @click="choose(c.key)"
          >{{ candidate === c.key ? '当前候选' : '选择并重新预演' }}</el-button
        >
      </div>
    </article>
    <h3 class="detail-section-title">当前匹配证据</h3>
    <el-descriptions :column="2" border
      ><el-descriptions-item label="名称 / 年份">一致（示例）</el-descriptions-item
      ><el-descriptions-item label="IMDb">tt0000000（合成）</el-descriptions-item
      ><el-descriptions-item label="长度硬约束">{{
        blocked ? '未通过' : '通过'
      }}</el-descriptions-item
      ><el-descriptions-item label="协议示例">BitTorrent v1</el-descriptions-item
      ><el-descriptions-item label="piece 证据">{{
        task.level === 'FULL_VERIFIED' ? '12,288 / 12,288 通过' : '未完成全量验证'
      }}</el-descriptions-item
      ><el-descriptions-item label="抽样 hash"
        >不作为执行依据</el-descriptions-item
      ></el-descriptions
    ></template
  >
  <template v-else-if="tab === '文件映射'"
    ><div class="section-heading">
      <h3>逐文件映射</h3>
      <el-button size="small" @click="emit('export', files, task.id + '-映射示例.json')"
        ><Download :size="14" />导出映射</el-button
      >
    </div>
    <article v-for="(f, i) in files" :key="f.source" class="mapping-card">
      <div class="mapping-title">
        <FileVideo :size="17" /><b>{{ f.source.split('/').pop() }}</b
        ><span>{{ f.size }}</span
        ><el-tag :type="f.action.includes('等待') ? 'danger' : 'success'">{{ f.action }}</el-tag>
      </div>
      <dl>
        <dt>源文件</dt>
        <dd>{{ f.source }}</dd>
        <dt>目标文件</dt>
        <dd>{{ f.target }}</dd>
      </dl>
      <div v-if="i === 0 && task.error === 'FILE_MAPPING_AMBIGUOUS'" class="mapping-fix">
        <el-select v-model="mapped" placeholder="请选择正确的源版本" aria-label="选择源文件"
          ><el-option label="Movie.01.BluRay.mkv · 10.8 GB" value="bluray" /><el-option
            label="Movie.01.WEB-DL.mkv · 9.2 GB（长度不符）"
            value="web"
            disabled /></el-select
        ><el-button type="primary" @click="mapFile">应用映射</el-button>
      </div>
    </article>
    <div v-if="repairScenario" class="detail-block">
      <b>缺失附件：poster.jpg</b>
      <p>影响 piece #2047–2050，与 S01E01.mkv 共享边界；预计补齐 128 MB。禁止使用占位文件。</p>
      <el-button @click="tab = '安全修复'">查看隔离计划</el-button>
    </div>
    <el-button :disabled="!!task.error || task.state !== 'AWAITING_CONFIRMATION'" @click="reverify"
      ><RotateCcw :size="15" />重新模拟验证</el-button
    ></template
  >
  <template v-else-if="tab === '时间线与资源'"
    ><h3>状态时间线</h3>
    <el-timeline
      ><el-timeline-item
        v-for="(e, i) in task.events"
        :key="i"
        :timestamp="`步骤 ${i + 1}`"
        placement="top"
        color="#216be5"
        >{{ e }}</el-timeline-item
      ></el-timeline
    >
    <h3 class="detail-section-title">操作资源登记 <span>演示记录</span></h3>
    <el-table :data="files"
      ><el-table-column prop="target" label="目标资源" min-width="240" /><el-table-column
        label="操作"
        width="90"
        ><template #default>硬链接</template></el-table-column
      ><el-table-column label="日志状态" width="175"
        ><template #default>{{
          ['SEEDING', 'CLIENT_VERIFYING'].includes(task.state) ? 'APPLIED' : 'INTENT_RECORDED'
        }}</template></el-table-column
      ></el-table
    >
    <p class="muted">trace_id：demo-{{ task.id.toLowerCase() }} · 只处理明确登记的演示资源。</p>
    <el-button @click="emit('export', { mode: 'demo', task, revision }, task.id + '-诊断示例.json')"
      ><Download :size="16" />导出诊断示例</el-button
    ></template
  >
  <template v-else
    ><h3>99% 安全修复</h3>
    <el-alert
      title="需要写入的目标必须先解除与源数据的硬链接关系。"
      type="warning"
      :closable="false"
    /><el-form label-position="top" class="form-stack"
      ><el-form-item label="修复模式"
        ><el-radio-group v-model="repair" class="repair-options"
          ><el-radio value="自动 piece 修复"
            >自动 piece 修复 <small>先隔离全部受影响文件，再补齐</small></el-radio
          ><el-radio value="仅文件级修复"
            >仅文件级修复 <small>只处理独立文件；不修改硬链接媒体</small></el-radio
          ><el-radio value="人工引导"
            >人工引导 <small>导出影响文件与建议步骤，等待人工处理</small></el-radio
          ></el-radio-group
        ></el-form-item
      ></el-form
    ><el-descriptions :column="1" border
      ><el-descriptions-item label="影响 piece">#2047–2050（合成示例）</el-descriptions-item
      ><el-descriptions-item label="影响文件">S01E01.mkv、poster.jpg</el-descriptions-item
      ><el-descriptions-item label="隔离空间">10.8 GB，可用 420 GB</el-descriptions-item
      ><el-descriptions-item label="预计补齐">128 MB</el-descriptions-item
      ><el-descriptions-item label="拒绝条件"
        >无法暂停、空间不足、所有权不明或无法隔离</el-descriptions-item
      ></el-descriptions
    >
    <div class="detail-block">
      <p>
        人工流程：暂停目标任务 → 核对资源登记 → 复制并原子替换目标媒体 → 确认独立 inode → 补齐 →
        重校验。
      </p>
      <el-button
        :disabled="!repairScenario || repair === '仅文件级修复'"
        type="primary"
        @click="runRepair"
        >演示修复流程</el-button
      >
      <p v-if="repair === '仅文件级修复'" class="red">
        当前示例跨文件 piece 涉及媒体，文件级修复已阻断。
      </p>
      <p v-if="!repairScenario" class="muted">请打开「森林之境」体验对应失败场景。</p>
    </div></template
  >
  <div class="detail-footer">
    <span><ShieldCheck :size="15" />仅模拟操作</span
    ><el-button
      v-if="task.state === 'CLIENT_VERIFYING'"
      type="primary"
      @click="
        finishVerification(task);
        ElMessage.success('模拟校验完成，任务已进入做种');
      "
      ><Check :size="16" />模拟校验完成</el-button
    ><el-button v-else-if="['SEEDING', 'DONE'].includes(task.state)" type="success" disabled
      >已确认做种</el-button
    ><el-button v-else :disabled="!canApprove(task)" type="primary" @click="approve"
      ><Play :size="15" />批准模拟执行</el-button
    >
  </div>
</template>
