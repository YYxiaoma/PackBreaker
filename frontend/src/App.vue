<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import {
  Box,
  LayoutDashboard,
  ListChecks,
  GitBranch,
  History,
  Globe,
  HardDrive,
  SlidersHorizontal,
  ShieldCheck,
  ScrollText,
  Settings,
  ArrowUpCircle,
  Plus,
  Search,
  Folder,
  Clapperboard,
  ChevronRight,
  ChevronLeft,
  Bell,
  CircleHelp,
  Sun,
  Moon,
  Menu,
  RefreshCw,
  Download,
  Activity,
  Layers,
  ArrowUpRight,
  X,
  Check,
  MoreHorizontal,
  LogOut,
} from '@lucide/vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import {
  seedTasks,
  stateNames,
  levelNames,
  filterTasks,
  pauseTask,
  resumeTask,
  type Task,
} from './demo';
import TaskDetail from './components/TaskDetail.vue';
import TaskCenter from './components/TaskCenter.vue';
import PreflightReviewCenter from './components/PreflightReviewCenter.vue';
import Management from './components/Management.vue';
import AuthGate from './components/AuthGate.vue';
import { AUTH_REQUIRED_EVENT } from './api/client';
import { useAuthStore } from './stores/auth';

const auth = useAuthStore();

function handleAuthRequired(): void {
  auth.markUnauthenticated();
}

onMounted(() => {
  window.addEventListener(AUTH_REQUIRED_EVENT, handleAuthRequired);
  void auth.bootstrap();
});
onUnmounted(() => window.removeEventListener(AUTH_REQUIRED_EVENT, handleAuthRequired));

const nav = [
  { name: '总览', icon: LayoutDashboard },
  { name: '任务中心', icon: ListChecks },
  { name: '预演与确认', icon: GitBranch },
  { name: '历史辅种', icon: History },
  { name: '站点管理', icon: Globe, group: '连接与规则' },
  { name: '下载器', icon: HardDrive },
  { name: '规则配置', icon: SlidersHorizontal },
  { name: '清理与对账', icon: ShieldCheck, group: '系统' },
  { name: '日志', icon: ScrollText },
  { name: '系统设置', icon: Settings },
  { name: '升级中心', icon: ArrowUpCircle },
];
const route = ref(location.hash.slice(1) ? decodeURIComponent(location.hash.slice(1)) : '任务中心');
const tasks = ref(seedTasks()),
  tab = ref('全部任务'),
  query = ref(''),
  site = ref(''),
  client = ref(''),
  page = ref(1),
  selected = ref<Task[]>([]),
  dark = ref(localStorage.getItem('pb-theme') === 'dark'),
  mobile = ref(false);
const active = ref<Task>(),
  drawer = ref(false),
  create = ref(false),
  help = ref(false);
const form = ref({
  name: '星际旅人 · 三部曲',
  kind: '电影大包',
  source: 'qBittorrent',
  client: 'qBittorrent',
  site: 'M-Team',
});
const filtered = computed(() =>
  filterTasks(
    tasks.value,
    route.value === '预演与确认' ? '待确认' : tab.value,
    query.value,
    site.value,
    client.value,
  ),
);
const displayed = computed(() => filtered.value.slice((page.value - 1) * 6, page.value * 6));
const waiting = computed(
  () => tasks.value.filter((t) => t.state === 'AWAITING_CONFIRMATION').length,
);
const running = computed(
  () =>
    tasks.value.filter((t) => ['VERIFYING', 'SEARCHING', 'CLIENT_VERIFYING'].includes(t.state))
      .length,
);
watch([tab, query, site, client, route], () => {
  page.value = 1;
  selected.value = [];
});
watch(route, (v) => {
  location.hash = encodeURIComponent(v);
  document.title = `PackBreaker · ${v}`;
  mobile.value = false;
});
window.addEventListener('hashchange', () => {
  const next = decodeURIComponent(location.hash.slice(1));
  if (nav.some((n) => n.name === next)) route.value = next;
});
watch(
  dark,
  (v) => {
    document.documentElement.classList.toggle('dark', v);
    localStorage.setItem('pb-theme', v ? 'dark' : 'light');
  },
  { immediate: true },
);
const tone = (t: Task) =>
  t.level === 'BLOCKED'
    ? 'danger'
    : t.state === 'AWAITING_CONFIRMATION'
      ? 'warning'
      : ['SEEDING', 'DONE'].includes(t.state)
        ? 'success'
        : ['PAUSED', 'CANCELLED'].includes(t.state)
          ? 'info'
          : 'primary';
function open(t: Task) {
  active.value = t;
  drawer.value = true;
}
function download(data: unknown, name: string) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }),
  );
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}
function createTask() {
  if (!form.value.name.trim()) {
    ElMessage.warning('请填写任务名称');
    return;
  }
  const existing = tasks.value.find(
    (t) =>
      t.name === form.value.name.trim() &&
      t.client === form.value.client &&
      t.site === form.value.site,
  );
  if (existing) {
    create.value = false;
    open(existing);
    ElMessage.info('已返回相同演示任务，未重复创建');
    return;
  }
  const task: Task = {
    id: `PB-${249 + tasks.value.length - 10}`,
    name: form.value.name.trim(),
    size: '72.8 GB',
    site: form.value.site,
    client: form.value.client,
    state: 'AWAITING_CONFIRMATION',
    level: 'FULL_VERIFIED',
    progress: 100,
    kind: form.value.kind,
    units: form.value.kind === '剧集拆包' ? 8 : 3,
    events: ['演示任务已创建', '已模拟解析、搜索与完整 piece 验证，等待预演确认'],
  };
  tasks.value.unshift(task);
  create.value = false;
  route.value = '任务中心';
  tab.value = '全部任务';
  open(task);
  ElMessage.success('演示预演已生成');
}
function batch() {
  const n = selected.value.filter(pauseTask).length;
  ElMessage.info(n ? `已暂停 ${n} 个演示任务` : '所选任务没有可暂停的活动任务');
}
function toggleSelected(t: Task) {
  selected.value = selected.value.includes(t)
    ? selected.value.filter((v) => v !== t)
    : [...selected.value, t];
}
function historyTask(name: string) {
  form.value = {
    name,
    kind: name.includes('剧集') ? '剧集拆包' : '历史影片',
    source: 'qBittorrent',
    client: 'qBittorrent',
    site: 'M-Team',
  };
  createTask();
}
async function logout(): Promise<void> {
  try {
    await ElMessageBox.confirm('退出当前管理员会话？未保存的页面输入将丢失。', '退出登录', {
      confirmButtonText: '退出登录',
      cancelButtonText: '取消',
      type: 'warning',
    });
    await auth.logout();
    ElMessage.success('管理员会话已退出');
  } catch {}
}

async function cancel(t: Task) {
  try {
    await ElMessageBox.confirm(
      `取消 ${t.id}「${t.name}」的演示任务。模拟移除其下载器任务，并回滚操作日志登记的链接；源文件不在影响范围内。`,
      '确认取消与回滚',
      { confirmButtonText: '确认取消', cancelButtonText: '保留任务', type: 'warning' },
    );
    t.state = 'CANCELLED';
    t.events.push('模拟回滚本系统登记资源，源数据保持不变');
    ElMessage.success('演示任务已取消');
  } catch {}
}
</script>

<template>
  <AuthGate v-if="auth.loading || !auth.authenticated" />
  <div v-else class="app-shell">
    <div v-if="mobile" class="sidebar-mask" @click="mobile = false"></div>
    <aside class="sidebar" :class="{ visible: mobile }">
      <a class="brand" href="#任务中心" @click="route = '任务中心'"
        ><span class="brand-icon"><Box :size="25" /></span>PackBreaker</a
      >
      <div class="workspace"><i class="dot"></i> 本地工作空间 <span>⌃</span></div>
      <nav>
        <template v-for="item in nav" :key="item.name"
          ><p v-if="item.group" class="nav-group">{{ item.group }}</p>
          <button :class="['nav-item', { active: route === item.name }]" @click="route = item.name">
            <component :is="item.icon" :size="19" /><span>{{ item.name }}</span>
          </button></template
        >
      </nav>
      <div class="sidebar-bottom">
        <button class="nav-item" @click="help = true">
          <LayoutDashboard :size="18" />原型体验指南
        </button>
        <div class="profile">
          <span class="avatar">A</span>
          <div><strong>管理员</strong><small>本地安全会话</small></div>
          <button class="icon-button" aria-label="退出登录" title="退出登录" @click="logout">
            <LogOut :size="17" />
          </button>
        </div>
      </div>
    </aside>
    <div class="main-shell">
      <header class="topbar">
        <div class="breadcrumb">
          <button class="icon-button mobile-only" aria-label="展开导航" @click="mobile = true">
            <Menu :size="20" /></button
          ><span>工作空间</span><ChevronRight :size="15" /><b>{{ route }}</b>
        </div>
        <div class="top-actions">
          <span class="demo-tag">v0.1 · M1 开发</span
          ><button
            class="icon-button"
            :aria-label="dark ? '切换浅色主题' : '切换深色主题'"
            @click="dark = !dark"
          >
            <Sun v-if="dark" :size="18" /><Moon v-else :size="18" /></button
          ><button
            class="icon-button notification"
            aria-label="待确认通知"
            @click="route = '预演与确认'"
          >
            <Bell :size="18" /></button
          ><button class="icon-button" aria-label="体验指南" @click="help = true">
            <CircleHelp :size="18" />
          </button>
        </div>
      </header>
      <main>
        <div class="page-heading">
          <div>
            <h1>{{ route }}</h1>
            <p>
              {{
                route === '任务中心'
                  ? '拆包、验证与辅种，所有进展一目了然。'
                  : route === '预演与确认'
                    ? '查看匹配证据与文件计划，再决定是否执行。'
                    : 'PackBreaker · 自动拆包辅种工作空间'
              }}
            </p>
          </div>
          <div class="heading-actions">
            <el-button @click="route = '历史辅种'"><History :size="16" />历史扫描</el-button
            ><el-button type="primary" @click="create = true"
              ><Plus :size="17" />新建拆包任务</el-button
            >
          </div>
        </div>
        <div class="demo-notice">
          <span class="dot"></span>混合研发模式
          <span
            >「任务中心」和「预演与确认」已接入真实 SQLite/API 数据；总览仍为合成样例。任务
            Analyze、下载器、管理员认证与 API Token 已接入真实后端。</span
          ><button @click="help = true">体验指南 <ArrowUpRight :size="13" /></button>
        </div>
        <TaskCenter v-if="route === '任务中心'" />
        <PreflightReviewCenter v-else-if="route === '预演与确认'" />
        <template v-else-if="route === '总览'">
          <div class="stats">
            <div class="stat">
              <div>演示任务<Layers :size="20" /></div>
              <strong>{{ String(tasks.length).padStart(2, '0') }}</strong
              ><small>电影 / 剧集 / 历史扫描</small>
            </div>
            <div class="stat">
              <div>正在处理<Activity :size="20" /></div>
              <strong>{{ String(running).padStart(2, '0') }}</strong
              ><small>搜索与完整校验进行中</small>
            </div>
            <div class="stat">
              <div>待人工确认<GitBranch :size="20" /></div>
              <strong>{{ String(waiting).padStart(2, '0') }}</strong
              ><small>执行确认 · 文件映射 · 安全修复</small>
            </div>
            <div class="stat">
              <div>数据复用示例<HardDrive :size="20" /></div>
              <strong>2.86 <em>TB</em></strong
              ><small>预计新增占用 128 MB</small>
            </div>
          </div>
          <div v-if="route === '总览'" class="overview-grid">
            <section class="panel">
              <h3>近 7 日辅种趋势 <small>演示统计</small></h3>
              <div class="bar-chart">
                <div v-for="(n, i) in [32, 50, 38, 70, 55, 86, 64]" :key="i">
                  <span>{{ n }}</span
                  ><i :style="{ height: n + 'px' }"></i
                  ><small>09/{{ String(i + 1).padStart(2, '0') }}</small>
                </div>
              </div>
            </section>
            <section class="panel">
              <h3>需要您的关注</h3>
              <button class="attention" @click="route = '预演与确认'">
                <GitBranch :size="21" />
                <div>
                  <b>{{ waiting }} 个任务等待审核</b>
                  <p>含 1 个文件映射歧义</p>
                </div>
                <ChevronRight :size="18" /></button
              ><button class="attention" @click="route = '清理与对账'">
                <ShieldCheck :size="21" />
                <div>
                  <b>1 项恢复检查待处理</b>
                  <p>查看资源登记与对账结果</p>
                </div>
                <ChevronRight :size="18" />
              </button>
            </section>
          </div>
          <section class="task-section">
            <div class="section-heading">
              <h2>拆包与辅种<small>电影 / 剧集 / 历史扫描</small></h2>
              <div>
                <button
                  class="icon-button"
                  aria-label="刷新演示列表"
                  @click="ElMessage.success('演示列表已刷新')"
                >
                  <RefreshCw :size="18" /></button
                ><button
                  class="icon-button"
                  aria-label="导出演示任务"
                  @click="download(filtered, 'PackBreaker-演示任务.json')"
                >
                  <Download :size="18" />
                </button>
              </div>
            </div>
            <div class="tabs">
              <button
                v-for="name in ['全部任务', '进行中', '待确认', '已完成', '异常 / 暂停']"
                :key="name"
                :class="{ chosen: tab === name }"
                @click="tab = name"
              >
                {{ name }}<small>{{ filterTasks(tasks, name, '', '', '').length }}</small>
              </button>
            </div>
            <div class="filters">
              <el-input
                v-model="query"
                placeholder="搜索任务名称、ID…"
                clearable
                aria-label="搜索任务"
                ><template #prefix><Search :size="17" /></template></el-input
              ><el-select v-model="site" placeholder="全部站点" clearable aria-label="筛选站点"
                ><el-option
                  v-for="s in ['M-Team', 'HDTime', 'HHClub']"
                  :key="s"
                  :value="s" /></el-select
              ><el-select
                v-model="client"
                placeholder="全部下载器"
                clearable
                aria-label="筛选下载器"
                ><el-option
                  v-for="c in ['qBittorrent', 'Transmission']"
                  :key="c"
                  :value="c" /></el-select
              ><el-button :disabled="!selected.length" @click="batch"
                ><ListChecks :size="16" />批量暂停<span v-if="selected.length"
                  >({{ selected.length }})</span
                ></el-button
              >
            </div>
            <div class="task-table">
              <el-table
                :data="displayed"
                row-key="id"
                @selection-change="(rows: Task[]) => (selected = rows)"
                empty-text="没有符合条件的任务，请调整筛选或新建任务。"
                ><el-table-column type="selection" width="43" /><el-table-column
                  label="任务名称 / 来源"
                  min-width="285"
                  ><template #default="{ row }"
                    ><div class="task-title">
                      <span :class="['file-icon', tone(row)]"
                        ><Clapperboard v-if="row.kind === '剧集拆包'" :size="21" /><Folder
                          v-else
                          :size="21"
                      /></span>
                      <div>
                        <button @click="open(row)">{{ row.name }}</button
                        ><small>{{ row.id }} · {{ row.size }} · {{ row.kind }}</small>
                      </div>
                    </div></template
                  ></el-table-column
                ><el-table-column prop="site" label="站点" min-width="108" /><el-table-column
                  prop="client"
                  label="目标下载器"
                  min-width="138"
                /><el-table-column label="当前状态" min-width="154"
                  ><template #default="{ row }"
                    ><el-tag :type="tone(row)" effect="light"
                      ><span class="status-dot">●</span
                      >{{
                        row.error === 'FILE_MAPPING_AMBIGUOUS'
                          ? '待文件映射'
                          : stateNames[row.state as keyof typeof stateNames]
                      }}</el-tag
                    ><small class="cell-note">{{
                      levelNames[row.level as keyof typeof levelNames]
                    }}</small></template
                  ></el-table-column
                ><el-table-column label="处理进度" min-width="145"
                  ><template #default="{ row }"
                    ><div class="progress-cell">
                      <span>{{ row.level === 'BLOCKED' ? '已阻断' : row.progress + '%' }}</span
                      ><el-progress
                        :percentage="row.progress"
                        :show-text="false"
                        :stroke-width="4"
                        :color="
                          tone(row) === 'success'
                            ? '#168765'
                            : tone(row) === 'warning'
                              ? '#b87a0b'
                              : '#246ee9'
                        "
                      /></div></template></el-table-column
                ><el-table-column label="更新 / 操作" min-width="148"
                  ><template #default="{ row }"
                    ><div class="row-actions">
                      <small>14:{{ String(32 - tasks.indexOf(row) * 2).padStart(2, '0') }}</small
                      ><el-button link type="primary" @click="open(row)">{{
                        row.state === 'AWAITING_CONFIRMATION' ? '审核' : '详情'
                      }}</el-button
                      ><el-dropdown trigger="click"
                        ><button class="icon-button" :aria-label="`${row.id}更多操作`">
                          <MoreHorizontal :size="18" /></button
                        ><template #dropdown
                          ><el-dropdown-menu
                            ><el-dropdown-item
                              :disabled="
                                !['SEARCHING', 'VERIFYING', 'CLIENT_VERIFYING'].includes(row.state)
                              "
                              @click="pauseTask(row)"
                              >暂停任务</el-dropdown-item
                            ><el-dropdown-item
                              :disabled="row.state !== 'PAUSED'"
                              @click="resumeTask(row)"
                              >恢复任务</el-dropdown-item
                            ><el-dropdown-item
                              :disabled="row.state === 'CANCELLED'"
                              @click="cancel(row)"
                              >取消与回滚</el-dropdown-item
                            ></el-dropdown-menu
                          ></template
                        ></el-dropdown
                      >
                    </div></template
                  ></el-table-column
                ></el-table
              >
            </div>
            <div class="mobile-task-list">
              <article v-for="t in displayed" :key="t.id" class="mobile-task">
                <div class="mobile-task-heading">
                  <el-checkbox
                    :model-value="selected.includes(t)"
                    :aria-label="'选择' + t.id"
                    @change="toggleSelected(t)"
                  /><span :class="['file-icon', tone(t)]"><Folder :size="20" /></span>
                  <div>
                    <button @click="open(t)">{{ t.name }}</button
                    ><small>{{ t.id }} · {{ t.size }} · {{ t.kind }}</small>
                  </div>
                </div>
                <div class="mobile-task-meta">
                  <span>{{ t.site }} · {{ t.client }}</span
                  ><el-tag :type="tone(t)">{{ stateNames[t.state] }}</el-tag>
                </div>
                <el-progress :percentage="t.progress" :stroke-width="4" />
                <div class="mobile-task-bottom">
                  <span>{{ levelNames[t.level] }}</span
                  ><el-button v-if="t.state === 'PAUSED'" link @click="resumeTask(t)"
                    >恢复</el-button
                  ><el-button
                    v-else-if="['VERIFYING', 'SEARCHING', 'CLIENT_VERIFYING'].includes(t.state)"
                    link
                    @click="pauseTask(t)"
                    >暂停</el-button
                  ><el-button type="primary" link @click="open(t)"
                    >{{ t.state === 'AWAITING_CONFIRMATION' ? '审核计划' : '查看详情' }}
                    <ChevronRight :size="14"
                  /></el-button>
                </div>
              </article>
              <el-empty v-if="!displayed.length" description="没有符合条件的任务" />
            </div>
            <div class="pagination">
              <span
                >显示 {{ filtered.length ? (page - 1) * 6 + 1 : 0 }}–{{
                  Math.min(page * 6, filtered.length)
                }}
                条 / 共 {{ filtered.length }} 个任务</span
              >
              <div>
                <span>第 {{ page }} 页</span
                ><button
                  class="icon-button"
                  :disabled="page === 1"
                  aria-label="上一页"
                  @click="page--"
                >
                  <ChevronLeft :size="17" /></button
                ><b>{{ page }}</b
                ><button
                  class="icon-button"
                  :disabled="page * 6 >= filtered.length"
                  aria-label="下一页"
                  @click="page++"
                >
                  <ChevronRight :size="17" />
                </button>
              </div>
            </div>
          </section>
          <div class="connection-strip">
            <section>
              <h3>
                站点连接
                <button @click="route = '站点管理'">管理站点 <ArrowUpRight :size="14" /></button>
              </h3>
              <div class="connections">
                <span class="mini-logo">MT</span>M-Team <i class="dot"></i
                ><span class="mini-logo">HD</span>HDTime <i class="dot"></i
                ><span class="mini-logo muted">HH</span><span class="muted">HHClub · 待确认</span>
              </div>
            </section>
            <section>
              <h3>下载器状态 <el-tag type="success">2 / 2 演示在线</el-tag></h3>
              <div class="connections">
                <HardDrive :size="18" />qBittorrent <span class="green">在线</span
                ><small>156 做种</small><HardDrive :size="18" />Transmission
                <span class="green">在线</span><small>92 做种</small>
              </div>
            </section>
          </div>
        </template>
        <Management
          v-show="!['任务中心', '预演与确认', '总览'].includes(route)"
          :page="route"
          :tasks="tasks"
          @export="download"
          @open="open"
          @create-history="historyTask"
        />
        <footer>
          <span><ShieldCheck :size="15" />源数据只读 <i>·</i> 自动修复关闭</span
          ><span>PackBreaker / 原型 v0.1</span>
        </footer>
      </main>
    </div>
    <el-drawer v-model="drawer" :title="active?.name" size="min(900px, 100vw)"
      ><TaskDetail v-if="active" :task="active" @export="download"
    /></el-drawer>
    <el-dialog v-model="create" title="新建拆包任务" width="min(560px, 94vw)"
      ><el-alert
        title="从合成源任务生成演示预演，不连接真实下载器。"
        type="info"
        :closable="false" /><el-form label-position="top" class="form-stack"
        ><el-form-item label="任务名称" required
          ><el-input v-model="form.name" maxlength="80"
        /></el-form-item>
        <div class="form-grid">
          <el-form-item label="处理类型"
            ><el-select v-model="form.kind"
              ><el-option
                v-for="s in ['电影大包', '剧集拆包', '历史影片']"
                :key="s"
                :value="s" /></el-select></el-form-item
          ><el-form-item label="源下载器"
            ><el-select v-model="form.source"
              ><el-option value="qBittorrent" /><el-option
                value="Transmission" /></el-select></el-form-item
          ><el-form-item label="目标站点"
            ><el-select v-model="form.site"
              ><el-option value="M-Team" /><el-option value="HDTime" /></el-select></el-form-item
          ><el-form-item label="目标下载器"
            ><el-select v-model="form.client"
              ><el-option value="qBittorrent" /><el-option value="Transmission" /></el-select
          ></el-form-item>
        </div>
        <div class="safety-note">
          <ShieldCheck :size="18" />默认人工确认 · 完整校验 · 源文件只读
        </div></el-form
      ><template #footer
        ><el-button @click="create = false">取消</el-button
        ><el-button type="primary" @click="createTask"
          >生成演示预演 <ChevronRight :size="16" /></el-button></template
    ></el-dialog>
    <el-dialog v-model="help" title="欢迎体验 PackBreaker" width="min(630px, 94vw)"
      ><p class="help-intro">按浅色控制台方案制作，用真实操作路径检查需求是否完整。</p>
      <ol class="guide">
        <li>
          <b>体验安全辅种</b>
          <p>打开「深空纪事」的审核，查看候选、文件映射与校验等级，再批准模拟执行。</p>
        </li>
        <li>
          <b>检查异常处理</b>
          <p>打开「远山回声」查看映射歧义；「森林之境」展示缺失文件与安全修复。</p>
        </li>
        <li>
          <b>检查日常管理</b>
          <p>创建任务、筛选与批量暂停，体验历史扫描、配置与对账页面。</p>
        </li>
      </ol>
      <el-alert
        title="管理员认证、API Token 与下载器配置已接入真实后端；任务、站点搜索、piece 验证、生产硬链接及运维操作仍为演示或待开发能力。"
        type="info"
        :closable="false"
      /><template #footer
        ><el-button type="primary" @click="help = false">开始体验</el-button></template
      ></el-dialog
    >
  </div>
</template>
