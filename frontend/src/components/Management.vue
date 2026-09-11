<script setup lang="ts">
import { computed, reactive, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import {
  Globe,
  HardDrive,
  Plus,
  Settings2,
  ShieldCheck,
  Check,
  Search,
  FolderSearch,
  Play,
  Pause,
  Download,
  RefreshCw,
  AlertTriangle,
  Activity,
  Server,
  Bell,
  LockKeyhole,
  ArrowUpCircle,
  FileText,
  RotateCcw,
  Trash2,
} from '@lucide/vue';
import type { Task } from '../demo';
import DownloaderManagement from './DownloaderManagement.vue';
import ApiTokenManagement from './AutomationAccessManagement.vue';
import NotificationManagement from './NotificationManagement.vue';
const props = defineProps<{ page: string; tasks: Task[] }>();
const emit = defineEmits<{ export: [unknown, string]; open: [Task]; createHistory: [string] }>();
interface Connection {
  name: string;
  type: string;
  url: string;
  enabled: boolean;
  automation: boolean;
  configured: boolean;
  status: string;
  rate: number;
  timeout: number;
  retries: number;
}
const sites = ref<Connection[]>([
  {
    name: 'M-Team',
    type: '官方 API',
    url: 'https://mteam.example.invalid',
    enabled: true,
    automation: false,
    configured: true,
    status: '演示连接正常',
    rate: 3,
    timeout: 20,
    retries: 2,
  },
  {
    name: 'HDTime',
    type: 'NexusPHP',
    url: 'https://hdtime.example.invalid',
    enabled: true,
    automation: false,
    configured: true,
    status: '演示连接正常',
    rate: 5,
    timeout: 20,
    retries: 2,
  },
  {
    name: 'HHClub',
    type: '待确认',
    url: 'https://hhclub.example.invalid',
    enabled: false,
    automation: false,
    configured: false,
    status: '适配方式待确认',
    rate: 5,
    timeout: 20,
    retries: 2,
  },
]);
const connectionDialog = ref(false),
  editing = ref<Connection>();
const draft = reactive<Connection>({
  name: '',
  type: '',
  url: '',
  enabled: false,
  automation: false,
  configured: false,
  status: '未测试',
  rate: 3,
  timeout: 20,
  retries: 2,
});
function editConnection(item?: Connection) {
  editing.value = item;
  Object.assign(
    draft,
    item ?? {
      name: '',
      type: 'NexusPHP',
      url: 'https://service.example.invalid',
      enabled: false,
      automation: false,
      configured: false,
      status: '未测试',
      rate: 3,
      timeout: 20,
      retries: 2,
    },
  );
  connectionDialog.value = true;
}
function saveConnection() {
  if (!draft.name.trim() || !/^https?:\/\//.test(draft.url)) {
    ElMessage.warning('请输入名称和合法的 HTTP / HTTPS 地址');
    return;
  }
  const list = sites.value;
  if (list.some((c) => c !== editing.value && c.name === draft.name.trim())) {
    ElMessage.warning('名称已存在');
    return;
  }
  if (editing.value) Object.assign(editing.value, draft);
  else list.push({ ...draft, name: draft.name.trim() });
  connectionDialog.value = false;
  ElMessage.success('演示配置已保存，本次页面会话内有效');
}
function testConnection(c: Connection) {
  if (c.type === '待确认') {
    ElMessage.warning('HHClub 适配方式待确认，无法模拟通过');
    return;
  }
  c.configured = true;
  c.status = '演示连接正常';
  ElMessage.success(`${c.name} 模拟连接测试通过；未发起网络请求`);
}
async function removeConnection(c: Connection) {
  if (props.tasks.some((t) => t.site === c.name)) {
    ElMessage.warning('该连接已被演示任务引用，不能删除；可停用');
    return;
  }
  try {
    await ElMessageBox.confirm(`删除演示连接「${c.name}」？`, '确认删除', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    });
    sites.value.splice(sites.value.indexOf(c), 1);
  } catch {}
}
const rules = reactive({
  automation: false,
  skip: false,
  repair: '人工引导',
  tag: '大包',
  stable: 60,
  interval: 60,
  concurrency: 1,
  exclude: 'sample, trailer, extras',
  extensions: '.mkv, .mp4, .m2ts',
  episodes: '按集拆包',
  approval: '全部人工确认',
  timeout: 20,
  retries: 2,
});
const savedRules = ref('');
const scanDialog = ref(false),
  scanPath = ref('/data/movies'),
  scanKind = ref('影片'),
  scanExclude = ref('sample, trailer, .incomplete'),
  scanTypes = ref('.mkv, .mp4, .m2ts');
const scans = ref([
  {
    id: 'SCAN-001',
    path: '/data/movies',
    kind: '影片',
    status: '已完成',
    count: 128,
    added: 12,
    progress: 100,
    cursor: 128,
  },
  {
    id: 'SCAN-002',
    path: '/data/tv',
    kind: '剧集',
    status: '已暂停',
    count: 64,
    added: 8,
    progress: 62,
    cursor: 40,
  },
]);
function startScan() {
  if (!scanPath.value.startsWith('/data/') || scanPath.value.split('/').includes('..')) {
    ElMessage.warning('请选择 /data/ 下的安全目录');
    return;
  }
  const same = scans.value.find((s) => s.path === scanPath.value && s.status === '扫描中');
  if (same) {
    ElMessage.info('该目录已有演示扫描任务');
    scanDialog.value = false;
    return;
  }
  scans.value.unshift({
    id: `SCAN-${String(scans.value.length + 1).padStart(3, '0')}`,
    path: scanPath.value,
    kind: scanKind.value,
    status: '扫描中',
    count: 24,
    added: 0,
    progress: 0,
    cursor: 0,
  });
  scanDialog.value = false;
  ElMessage.success('演示扫描已创建，可点击「推进扫描」体验增量进度');
}
function advanceScan(scan: (typeof scans.value)[number]) {
  scan.progress = Math.min(100, scan.progress + 50);
  scan.cursor = Math.round((scan.count * scan.progress) / 100);
  if (scan.progress === 100) {
    scan.status = '已完成';
    scan.added = 3;
  }
  ElMessage.success(
    scan.progress === 100
      ? '扫描完成：3 个新增候选，21 个未变化项目已跳过'
      : '演示扫描检查点已更新',
  );
}
const reconciliation = ref(false),
  cleaned = ref(false),
  recovered = ref(false);
async function cleanup() {
  try {
    await ElMessageBox.confirm(
      '仅清理 OP-021 登记的 2 个未使用演示硬链接（0 B 数据）。OP-022 所有权未知的目录不在清理范围内，源媒体不在清理范围内。',
      '确认清理范围',
      { confirmButtonText: '清理登记资源', cancelButtonText: '取消', type: 'warning' },
    );
    cleaned.value = true;
    ElMessage.success('已模拟清理 2 个登记链接');
  } catch {}
}
const logLevel = ref(''),
  logQuery = ref(''),
  logTask = ref(''),
  logTime = ref('全部时间');
const baseLogs = [
  {
    level: 'INFO',
    time: '14:32:08',
    task: 'PB-248',
    stage: 'VERIFYING',
    message: '候选 piece 校验进度 72%，源文件快照一致',
  },
  {
    level: 'WARN',
    time: '14:28:32',
    task: 'PB-245',
    stage: 'MATCHING',
    message: 'FILE_MAPPING_AMBIGUOUS：源文件映射存在歧义，等待人工确认',
  },
  {
    level: 'INFO',
    time: '14:25:16',
    task: 'PB-247',
    stage: 'PREFLIGHT',
    message: '预演已生成，FULL_VERIFIED；等待用户确认',
  },
  {
    level: 'ERROR',
    time: '14:12:01',
    task: 'PB-241',
    stage: 'PREFLIGHT',
    message: 'CROSS_DEVICE_LINK：设备 ID 不一致，阻断硬链接',
  },
  {
    level: 'INFO',
    time: '14:09:26',
    task: 'PB-244',
    stage: 'SEEDING',
    message: '客户端校验 100%，模拟确认辅种完成',
  },
  {
    level: 'DEBUG',
    time: '14:08:00',
    task: 'PB-248',
    stage: 'SEARCHING',
    message: '站点缓存命中；鉴权字段已省略',
  },
];
const logs = computed(() =>
  [
    ...props.tasks.flatMap((t) =>
      t.events
        .slice(2)
        .map((e) => ({ level: 'INFO', time: '14:35:00', task: t.id, stage: t.state, message: e })),
    ),
    ...baseLogs,
  ].filter(
    (l) =>
      (!logLevel.value || l.level === logLevel.value) &&
      (!logTask.value || l.task === logTask.value) &&
      (!logQuery.value ||
        `${l.message} demo-${l.task} ${l.stage}`
          .toLowerCase()
          .includes(logQuery.value.toLowerCase())) &&
      (logTime.value !== '最近 15 分钟' || l.time >= '14:20:00'),
  ),
);
const settingTab = ref('常规'),
  settings = reactive({
    timezone: 'Asia/Shanghai',
    retention: 30,
    session: 120,
    notification: '任务完成与失败',
    port: 8000,
  });
const backupReady = ref(false),
  restore = ref(false),
  restoreConfirmed = ref(false),
  updateChecked = ref(false),
  docker = ref(false),
  updated = ref(false),
  failUpdate = ref(false);
async function update() {
  try {
    await ElMessageBox.confirm(
      '模拟从 v0.1.0 升级到 v0.1.1：创建备份 → 更新 → 健康检查；失败将展示回滚。不会访问 Docker 或重建容器。',
      '确认升级演示',
      { confirmButtonText: '开始演示', cancelButtonText: '取消', type: 'warning' },
    );
    updated.value = true;
    ElMessage.info(
      failUpdate.value ? '演示健康检查失败，已模拟回滚至 v0.1.0' : '演示升级完成，健康检查通过',
    );
  } catch {}
}
</script>
<template>
  <DownloaderManagement v-if="page === '下载器'" />
  <div v-else-if="page === '站点管理'">
    <div class="section-heading">
      <h2>已接入站点 <small>站点可用与自动化独立控制</small></h2>
      <el-button type="primary" @click="editConnection()"><Plus :size="15" />添加站点</el-button>
    </div>
    <div class="connection-cards">
      <article class="panel connection-card" v-for="c in sites" :key="c.name">
        <div class="card-title">
          <span class="connection-icon"><Globe :size="25" /></span>
          <div>
            <h3>{{ c.name }}</h3>
            <small>{{ c.type }}</small>
          </div>
          <el-switch
            v-model="c.enabled"
            :aria-label="'启用' + c.name"
            :disabled="c.type === '待确认'"
          />
        </div>
        <p class="endpoint">{{ c.url }}</p>
        <div class="connection-status">
          <span :class="['dot', { 'gray-dot': !c.configured }]"></span
          >{{ c.enabled ? c.status : '已停用'
          }}<el-tag v-if="!c.configured" type="info">未配置</el-tag>
        </div>
        <dl class="config-summary">
          <dt>凭证状态</dt>
          <dd>{{ c.configured ? '演示凭证 · 已脱敏' : '未配置' }}</dd>
          <dt>限流 / 超时</dt>
          <dd>{{ c.rate }} 秒 / {{ c.timeout }} 秒</dd>
          <dt>允许自动化</dt>
          <dd>
            <el-switch
              v-model="c.automation"
              size="small"
              :disabled="!c.enabled || !c.configured || c.type === '待确认'"
              :aria-label="c.name + '允许自动化'"
            />
          </dd>
        </dl>
        <div class="card-actions">
          <el-button size="small" @click="testConnection(c)"
            ><Activity :size="14" />模拟测试</el-button
          ><el-button size="small" @click="editConnection(c)"
            ><Settings2 :size="14" />配置</el-button
          ><el-button link type="danger" @click="removeConnection(c)">删除</el-button>
        </div>
      </article>
    </div>
    <div class="panel section-space">
      <h3>站点健康与可靠性</h3>
      <el-table :data="sites"
        ><el-table-column prop="name" label="站点" /><el-table-column label="缓存命中"
          ><template #default="{ row }">{{
            row.configured ? '68%（演示）' : '—'
          }}</template></el-table-column
        ><el-table-column label="熔断状态"
          ><template #default="{ row }">{{
            row.configured ? '关闭' : '未接入'
          }}</template></el-table-column
        ><el-table-column label="重试上限"
          ><template #default="{ row }">{{ row.retries }} 次</template></el-table-column
        ><el-table-column label="操作"
          ><template #default="{ row }"
            ><el-button
              link
              type="primary"
              :disabled="!row.configured"
              @click="ElMessage.success(row.name + ' 已模拟半开探测，连接恢复')"
              >模拟熔断恢复</el-button
            ></template
          ></el-table-column
        ></el-table
      >
      <p class="muted">HHClub 引擎与鉴权仍待确认；本页不会将其标记为已实现适配器。</p>
    </div>
  </div>
  <div v-else-if="page === '规则配置'" class="settings-layout">
    <div class="panel">
      <h3>触发与处理规则</h3>
      <el-form label-position="top"
        ><div class="form-grid">
          <el-form-item label="命中的分类 / 标签"><el-input v-model="rules.tag" /></el-form-item
          ><el-form-item label="源任务稳定期（秒）"
            ><el-input-number v-model="rules.stable" :min="30" :max="3600" /></el-form-item
          ><el-form-item label="轮询间隔（秒）"
            ><el-input-number v-model="rules.interval" :min="30" :max="3600" /></el-form-item
          ><el-form-item label="重磁盘验证并发"
            ><el-input-number v-model="rules.concurrency" :min="1" :max="4" /></el-form-item
          ><el-form-item label="剧集处理方式"
            ><el-select v-model="rules.episodes"
              ><el-option value="按集拆包" /><el-option
                value="按季拆包" /></el-select></el-form-item
          ><el-form-item label="确认策略"
            ><el-select v-model="rules.approval"
              ><el-option value="全部人工确认" /><el-option
                value="完整验证后按规则自动确认" /></el-select
          ></el-form-item>
        </div>
        <el-form-item label="媒体扩展名"><el-input v-model="rules.extensions" /></el-form-item
        ><el-form-item label="排除目录与关键词"><el-input v-model="rules.exclude" /></el-form-item>
        <div class="setting-row">
          <div>
            <b>启用自动任务触发</b>
            <p>仅影响演示配置，默认关闭</p>
          </div>
          <el-switch v-model="rules.automation" />
        </div>
        <el-button
          type="primary"
          @click="
            savedRules = JSON.stringify(rules);
            ElMessage.success('规则已保存在当前演示会话');
          "
          >保存演示规则</el-button
        ><span v-if="savedRules" class="saved-label">已保存</span></el-form
      >
    </div>
    <div class="panel">
      <h3>安全与修复策略</h3>
      <div class="setting-row">
        <div>
          <b>qB 允许跳过客户端校验</b>
          <p>仅完整 piece 验证通过时生效</p>
        </div>
        <el-switch v-model="rules.skip" />
      </div>
      <el-alert
        v-if="rules.skip"
        title="模拟配置已启用。CLIENT_CHECK_REQUIRED 和 Transmission 仍必须校验。"
        type="warning"
        :closable="false"
      /><el-form label-position="top" class="form-stack"
        ><el-form-item label="99% 修复默认模式"
          ><el-select v-model="rules.repair"
            ><el-option value="人工引导" /><el-option value="仅文件级修复" /><el-option
              value="自动 piece 修复" /></el-select></el-form-item
      ></el-form>
      <div
        class="locked-rule"
        v-for="s in [
          '源数据只读',
          '拒绝路径穿越与不安全路径',
          '清理仅限操作日志登记资源',
          '硬链接修复前必须隔离 inode',
          '抽样 hash 不作为执行依据',
        ]"
        :key="s"
      >
        <LockKeyhole :size="15" />{{ s }}
      </div>
      <p class="muted">自动匹配阈值待真实语料标定，本轮保持人工确认默认值。</p>
    </div>
  </div>
  <div v-else-if="page === '历史辅种'">
    <div class="section-heading">
      <h2>历史目录扫描 <small>增量识别影片与剧集，复用安全预演流程</small></h2>
      <el-button type="primary" @click="scanDialog = true"><Plus :size="15" />新建扫描</el-button>
    </div>
    <div class="stats mini-stats">
      <div class="stat">
        <div>扫描根目录</div>
        <strong>{{ scans.length }}</strong
        ><small>合成目录，无真实访问</small>
      </div>
      <div class="stat">
        <div>识别文件</div>
        <strong>{{ scans.reduce((a, s) => a + s.cursor, 0) }}</strong
        ><small>记录扫描检查点</small>
      </div>
      <div class="stat">
        <div>新增处理单元</div>
        <strong>{{ scans.reduce((a, s) => a + s.added, 0) }}</strong
        ><small>未变化文件自动跳过</small>
      </div>
      <div class="stat">
        <div>扫描方式</div>
        <strong class="small-strong">增量扫描</strong><small>支持暂停与断点续扫</small>
      </div>
    </div>
    <article class="panel scan-card" v-for="scan in scans" :key="scan.id">
      <div class="card-title">
        <FolderSearch :size="26" />
        <div>
          <h3>{{ scan.path }}</h3>
          <small>{{ scan.id }} · {{ scan.kind }} · 游标 {{ scan.cursor }} / {{ scan.count }}</small>
        </div>
        <el-tag :type="scan.status === '已完成' ? 'success' : 'primary'">{{ scan.status }}</el-tag>
      </div>
      <el-progress :percentage="scan.progress" :stroke-width="5" />
      <div class="scan-bottom">
        <span>新增 {{ scan.added }} 项 · 只处理新建或变化的内容</span>
        <div>
          <el-button v-if="scan.status === '扫描中'" size="small" @click="advanceScan(scan)"
            >推进扫描</el-button
          ><el-button
            v-if="scan.status === '扫描中' || scan.status === '已暂停'"
            size="small"
            @click="scan.status = scan.status === '已暂停' ? '扫描中' : '已暂停'"
            >{{ scan.status === '已暂停' ? '断点续扫' : '暂停' }}</el-button
          ><el-button
            v-if="scan.status === '已完成'"
            size="small"
            type="primary"
            plain
            @click="
              emit(
                'createHistory',
                scan.kind === '影片' ? '历史影片 · 扫描示例' : '历史剧集 · 扫描示例',
              )
            "
            >生成示例预演</el-button
          >
        </div>
      </div>
    </article>
  </div>
  <div v-else-if="page === '清理与对账'">
    <div class="settings-layout">
      <section class="panel">
        <h3>启动对账</h3>
        <p class="muted">
          检查 LINKING、ADDING、CLIENT_VERIFYING 等中断阶段，根据登记证据决定恢复或回滚。
        </p>
        <div class="health-list">
          <div><Server :size="18" />数据库检查点 <el-tag type="success">演示可读</el-tag></div>
          <div><HardDrive :size="18" />下载器真实状态 <el-tag type="info">模拟快照</el-tag></div>
          <div>
            <ShieldCheck :size="18" />资源身份与所有权
            <el-tag :type="reconciliation ? 'warning' : 'info'">{{
              reconciliation ? '1 项待人工' : '待对账'
            }}</el-tag>
          </div>
        </div>
        <el-button
          type="primary"
          @click="
            reconciliation = true;
            ElMessage.success('演示对账完成');
          "
          ><RefreshCw :size="15" />运行模拟对账</el-button
        >
      </section>
      <section class="panel">
        <h3>安全清理</h3>
        <p class="muted">预览影响范围后，只清理本系统创建且身份仍匹配的未使用资源。</p>
        <div class="cleanup-value">{{ cleaned ? 0 : 2 }} <span>个可清理链接</span></div>
        <p>释放目录项，不删除源媒体数据。</p>
        <el-button :disabled="!reconciliation || cleaned" type="danger" plain @click="cleanup"
          ><Trash2 :size="15" />预览并清理登记资源</el-button
        >
      </section>
    </div>
    <section v-if="reconciliation" class="panel section-space">
      <h3>对账结果 <small class="muted"> · 演示资源</small></h3>
      <div class="resource-row">
        <span class="file-icon"><RefreshCw :size="20" /></span>
        <div>
          <b>OP-020 · 下载器添加结果待确认</b>
          <p>
            {{
              recovered
                ? '已模拟查询到相同任务，补记结果；未重复添加'
                : '检查点 ADDING，查询目标 hash 后决定是否补记'
            }}
          </p>
        </div>
        <el-button :disabled="recovered" @click="recovered = true">{{
          recovered ? '已恢复' : '模拟恢复'
        }}</el-button>
      </div>
      <div class="resource-row">
        <span class="file-icon success"><Check :size="20" /></span>
        <div>
          <b>OP-021 · 2 个未使用链接</b>
          <p>{{ cleaned ? '已模拟清理' : '所有权已登记，设备 / inode 与记录一致' }}</p>
        </div>
        <el-tag type="success">{{ cleaned ? '已清理' : '可清理' }}</el-tag>
      </div>
      <div class="resource-row">
        <span class="file-icon warning"><AlertTriangle :size="20" /></span>
        <div>
          <b>OP-022 · 目录所有权无法确认</b>
          <p>/data/seeding/unclaimed · 无法证明由系统创建</p>
        </div>
        <el-tag type="danger">禁止删除</el-tag>
      </div>
    </section>
  </div>
  <div v-else-if="page === '日志'">
    <div class="section-heading">
      <h2>运行日志 <small>合成日志 · 凭证字段不进入日志</small></h2>
      <el-button @click="emit('export', logs, 'PackBreaker-日志示例.json')"
        ><Download :size="15" />导出当前结果</el-button
      >
    </div>
    <div class="filters log-filters">
      <el-input v-model="logQuery" placeholder="搜索内容、错误码、trace_id" clearable
        ><template #prefix><Search :size="16" /></template></el-input
      ><el-select v-model="logLevel" placeholder="全部级别" clearable
        ><el-option
          v-for="l in ['DEBUG', 'INFO', 'WARN', 'ERROR']"
          :key="l"
          :value="l" /></el-select
      ><el-select v-model="logTask" placeholder="全部任务" clearable
        ><el-option v-for="t in tasks" :key="t.id" :value="t.id" /></el-select
      ><el-select v-model="logTime"
        ><el-option value="全部时间" /><el-option value="最近 15 分钟"
      /></el-select>
    </div>
    <div class="panel log-panel">
      <article v-for="(l, i) in logs" :key="i" class="log-line">
        <time>{{ l.time }}</time
        ><el-tag
          :type="
            l.level === 'ERROR'
              ? 'danger'
              : l.level === 'WARN'
                ? 'warning'
                : l.level === 'DEBUG'
                  ? 'info'
                  : 'primary'
          "
          >{{ l.level }}</el-tag
        >
        <div>
          <b>{{ l.message }}</b
          ><small>{{ l.task }} · {{ l.stage }} · trace_id: demo-{{ l.task }}</small>
        </div>
      </article>
      <el-empty v-if="!logs.length" description="没有符合条件的日志" />
    </div>
  </div>
  <div v-else-if="page === '系统设置'" class="panel">
    <el-tabs v-model="settingTab"
      ><el-tab-pane
        v-for="s in ['常规', '通知', '安全与集成', '备份恢复']"
        :key="s"
        :name="s"
        :label="s"
    /></el-tabs>
    <div class="settings-content" v-if="settingTab === '常规'">
      <h3>常规设置</h3>
      <el-form label-position="top"
        ><div class="form-grid">
          <el-form-item label="显示时区"
            ><el-select v-model="settings.timezone"
              ><el-option value="Asia/Shanghai" /><el-option
                value="UTC" /></el-select></el-form-item
          ><el-form-item label="日志保留（天）"
            ><el-input-number v-model="settings.retention" :min="1" :max="365" /></el-form-item
          ><el-form-item label="会话有效期（分钟）"
            ><el-input-number v-model="settings.session" :min="15" :max="1440"
          /></el-form-item>
        </div>
        <el-button
          type="primary"
          @click="ElMessage.success('演示设置已保存；时间格式将在真实数据接入后应用')"
          >保存演示设置</el-button
        ></el-form
      >
      <h3 class="detail-section-title">健康状态</h3>
      <div class="health-list">
        <div v-for="h in ['进程存活', '数据库可写', '任务 Worker 就绪']" :key="h">
          <Check :size="17" />{{ h }}<el-tag type="success">演示正常</el-tag>
        </div>
      </div>
    </div>
    <div v-else-if="settingTab === '通知'" class="settings-content">
      <NotificationManagement />
    </div>
    <div v-else-if="settingTab === '安全与集成'" class="settings-content">
      <el-alert
        title="管理员会话、CSRF、API Token 与加密 secret store 已接入真实后端。API Token 明文只在创建时展示一次。"
        type="success"
        :closable="false"
      />
      <h3 class="detail-section-title">管理员与访问</h3>
      <div class="setting-row">
        <div>
          <b>单管理员安全会话</b>
          <p>Argon2id 强哈希 · 持久会话撤销 · CSRF · 登录双维度限速</p>
        </div>
        <el-tag type="success">已认证</el-tag>
      </div>
      <ApiTokenManagement />
      <h3 class="detail-section-title">下载完成 Webhook</h3>
      <code class="code-block">POST /api/v1/integrations/download-completed</code>
      <div class="check-grid">
        <div
          v-for="s in [
            'HMAC-SHA256 签名',
            '时间戳窗口 300 秒',
            'Nonce 防重放',
            'Idempotency-Key 去重',
          ]"
          :key="s"
        >
          <ShieldCheck :size="15" />{{ s }}
        </div>
      </div>
      <p class="muted">上方为计划契约，本原型未开放真实接口。</p>
    </div>
    <div v-else class="settings-content">
      <h3>备份与恢复</h3>
      <el-alert
        title="演示导出仅含合成配置，不是可用于生产恢复的数据库备份。"
        type="info"
        :closable="false"
      />
      <div class="backup-card">
        <FileText :size="28" />
        <div>
          <b>演示配置快照</b>
          <p>规则、非敏感设置 · 不含凭证</p>
        </div>
        <el-button
          type="primary"
          @click="
            backupReady = true;
            emit('export', { mode: 'prototype', rules, settings }, 'PackBreaker-演示配置.json');
          "
          >导出示例</el-button
        >
      </div>
      <el-button
        @click="
          restore = true;
          restoreConfirmed = false;
        "
        >体验恢复确认</el-button
      >
      <p v-if="backupReady" class="green">演示配置已导出。</p>
      <h3 class="detail-section-title">正式恢复流程（计划）</h3>
      <p class="muted">
        验证备份可读 → 检查数据库版本 → 暂停任务 → 恢复一致性快照 → 健康检查 →
        失败回滚。包含凭证的备份须再次加密，主密钥独立保管。
      </p>
    </div>
  </div>
  <div v-else-if="page === '升级中心'" class="settings-layout">
    <section class="panel">
      <div class="version-icon"><BoxIcon /></div>
      <h3>
        PackBreaker
        <span class="version-number">v{{ updated && !failUpdate ? '0.1.1' : '0.1.0' }}</span>
      </h3>
      <p class="muted">交互原型 · 演示版本</p>
      <div class="setting-row">
        <div>
          <b>启用 Docker 管理入口</b>
          <p>生产环境需显式挂载 docker.sock，具有 Docker 管理权限。</p>
        </div>
        <el-switch v-model="docker" />
      </div>
      <el-button
        @click="
          updateChecked = true;
          ElMessage.success('演示发现 v0.1.1');
        "
        ><RefreshCw :size="15" />模拟检查更新</el-button
      >
      <div v-if="updateChecked" class="update-release">
        <h3>v0.1.1 · 演示更新</h3>
        <p>改善任务审核体验与日志筛选；数据库版本兼容。</p>
        <el-checkbox v-model="failUpdate">模拟健康检查失败，体验回滚</el-checkbox>
        <div class="section-space">
          <el-button type="primary" :disabled="!docker" @click="update">模拟升级</el-button>
          <p v-if="!docker" class="muted">请先显式启用上方演示管理入口。</p>
        </div>
      </div>
    </section>
    <section class="panel">
      <h3>升级流程</h3>
      <el-steps
        direction="vertical"
        :active="updated ? 4 : 0"
        finish-status="success"
        class="update-steps"
        ><el-step title="创建并校验备份" description="数据库与非敏感配置" /><el-step
          title="更新应用"
          description="检查镜像与数据库版本" /><el-step
          title="健康检查"
          :description="updated && failUpdate ? '演示检查失败' : '存活、就绪与迁移状态'" /><el-step
          :title="updated && failUpdate ? '回滚至原版本' : '完成更新'"
          :description="updated ? '演示操作完成' : '保留可恢复备份'"
      /></el-steps>
    </section>
  </div>
  <el-dialog
    v-model="connectionDialog"
    :title="(editing ? '编辑' : '新增') + '站点'"
    width="min(600px, 94vw)"
    ><el-form label-position="top"
      ><div class="form-grid">
        <el-form-item label="名称" required
          ><el-input v-model="draft.name" maxlength="40" /></el-form-item
        ><el-form-item label="适配器类型"
          ><el-select v-model="draft.type"
            ><el-option
              v-for="s in ['官方 API', 'NexusPHP', '待确认']"
              :key="s"
              :value="s" /></el-select
        ></el-form-item>
      </div>
      <el-form-item label="管理地址" required
        ><el-input
          v-model="draft.url"
          placeholder="https://service.example.invalid" /></el-form-item
      ><el-form-item label="凭证"
        ><el-input type="password" disabled placeholder="原型不接收真实凭证"
      /></el-form-item>
      <div class="form-grid">
        <el-form-item label="请求间隔（秒）"
          ><el-input-number v-model="draft.rate" :min="1" :max="120" /></el-form-item
        ><el-form-item label="超时（秒）"
          ><el-input-number v-model="draft.timeout" :min="5" :max="120" /></el-form-item
        ><el-form-item label="重试上限"
          ><el-input-number v-model="draft.retries" :min="0" :max="5"
        /></el-form-item>
      </div>
      <el-alert
        title="此处只保存演示配置。测试连接不会访问填写的地址。"
        type="info"
        :closable="false" /></el-form
    ><template #footer
      ><el-button @click="connectionDialog = false">取消</el-button
      ><el-button type="primary" @click="saveConnection">保存演示配置</el-button></template
    ></el-dialog
  >
  <el-dialog v-model="scanDialog" title="新建历史扫描" width="min(560px, 94vw)"
    ><el-form label-position="top"
      ><el-form-item label="扫描根目录"><el-input v-model="scanPath" /></el-form-item
      ><el-form-item label="识别类型"
        ><el-radio-group v-model="scanKind"
          ><el-radio value="影片" /><el-radio value="剧集" /></el-radio-group></el-form-item
      ><el-form-item label="文件类型"><el-input v-model="scanTypes" /></el-form-item
      ><el-form-item label="排除规则"><el-input v-model="scanExclude" /></el-form-item
      ><el-alert
        title="模拟增量扫描，重复触发不重复创建活跃扫描；结果进入人工预演。"
        type="info"
        :closable="false" /></el-form
    ><template #footer
      ><el-button @click="scanDialog = false">取消</el-button
      ><el-button type="primary" @click="startScan">开始演示扫描</el-button></template
    ></el-dialog
  >
  <el-dialog v-model="restore" title="确认恢复范围（演示）" width="min(560px, 94vw)"
    ><el-alert
      title="将模拟替换演示配置并暂停活动任务；不修改真实配置或数据库。"
      type="warning"
      :closable="false"
    />
    <p>备份：packbreaker-demo-v0.1 · 完整性检查通过（合成结果）。</p>
    <el-checkbox v-model="restoreConfirmed">我已了解恢复范围及备份要求</el-checkbox
    ><template #footer
      ><el-button @click="restore = false">取消</el-button
      ><el-button
        :disabled="!restoreConfirmed"
        type="primary"
        @click="
          restore = false;
          ElMessage.success('已完成恢复确认演示；未实际替换配置');
        "
        >模拟恢复</el-button
      ></template
    ></el-dialog
  >
</template>

<script lang="ts">
import { defineComponent, h } from 'vue';
import { Box } from '@lucide/vue';
const BoxIcon = defineComponent({ setup: () => () => h(Box, { size: 35 }) });
</script>
