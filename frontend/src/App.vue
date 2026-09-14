<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue';
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
  ChevronRight,
  Bell,
  CircleHelp,
  Sun,
  Moon,
  Menu,
  ArrowUpRight,
  X,
  LogOut,
} from '@lucide/vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { seedTasks, type Task } from './demo';
import TaskDetail from './components/TaskDetail.vue';
import TaskCenter from './components/TaskCenter.vue';
import PreflightReviewCenter from './components/PreflightReviewCenter.vue';
import OperationalOverview from './components/OperationalOverview.vue';
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
  open(task);
  ElMessage.success('演示预演已生成');
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
          <span class="demo-tag">v0.1 · M6 开发</span
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
            >「任务中心」「预演与确认」「总览」和「日志」已接入真实 SQLite/API 或本地运维数据；
            其余仍在逐步移除合成演示。</span
          ><button @click="help = true">体验指南 <ArrowUpRight :size="13" /></button>
        </div>
        <TaskCenter v-if="route === '任务中心'" />
        <PreflightReviewCenter v-else-if="route === '预演与确认'" />
        <OperationalOverview v-else-if="route === '总览'" />
        <Management
          v-show="!['任务中心', '预演与确认', '总览'].includes(route)"
          :page="route"
          @export="download"
          @open="open"
          @create-history="historyTask"
          @navigate="route = $event"
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
        title="管理员认证、API Token、下载器配置与 operation journal 清理/对账报告已接入真实后端；站点搜索、piece 修复及部分日常管理仍包含演示或待开发能力。"
        type="info"
        :closable="false"
      /><template #footer
        ><el-button type="primary" @click="help = false">开始体验</el-button></template
      ></el-dialog
    >
  </div>
</template>
