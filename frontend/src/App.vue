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
  ShieldCheck,
  ScrollText,
  Settings,
  ChevronRight,
  Bell,
  Sun,
  Moon,
  Menu,
  LogOut,
  Sparkles,
} from '@lucide/vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import TaskDefinitionCenter from './components/TaskDefinitionCenter.vue';
import PreflightReviewCenter from './components/PreflightReviewCenter.vue';
import OperationalOverview from './components/OperationalOverview.vue';
import Management from './components/Management.vue';
import AuthGate from './components/AuthGate.vue';
import VersionPopover from './components/VersionPopover.vue';
import { AUTH_REQUIRED_EVENT } from './api/client';
import { useAuthStore } from './stores/auth';

const auth = useAuthStore();
const versionPopover = ref<InstanceType<typeof VersionPopover> | null>(null);

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
  { name: '站点管理', icon: Globe },
  { name: '下载器', icon: HardDrive },
  { name: '清理与对账', icon: ShieldCheck },
  { name: '日志', icon: ScrollText },
  { name: '系统设置', icon: Settings },
];
const pageCopy: Record<string, { eyebrow: string; description: string }> = {
  总览: {
    eyebrow: '运行态势',
    description: '聚合健康、任务、备份与依赖状态，快速定位需要关注的系统信号。',
  },
  任务中心: {
    eyebrow: '自动化工作流',
    description: '查看任务状态、分析进度与安全操作入口，保持处理链路清晰可追踪。',
  },
  预演与确认: {
    eyebrow: '人工审核',
    description: '集中检查候选证据、当前性与风险，在执行前完成最终确认。',
  },
  历史辅种: {
    eyebrow: '历史资源',
    description: '扫描既有媒体并转换为受控任务，持续补齐可安全复用的辅种机会。',
  },
  站点管理: {
    eyebrow: '连接与规则',
    description: '管理 PT 站点连接、凭证状态、能力探测与可靠性保护。',
  },
  下载器: {
    eyebrow: '连接与规则',
    description: '统一管理下载器实例、路径映射、连接探测与运行边界。',
  },
  清理与对账: {
    eyebrow: '安全维护',
    description: '预览可清理记录与对账风险，只在证据充分时执行受控维护。',
  },
  日志: {
    eyebrow: '可观测性',
    description: '检索脱敏运行日志并导出受控窗口，辅助定位任务与依赖异常。',
  },
  系统设置: {
    eyebrow: '系统配置',
    description: '管理通知、安全集成、自动化访问与数据库备份策略。',
  },
};
const initialRoute = location.hash.slice(1) ? decodeURIComponent(location.hash.slice(1)) : '总览';
const route = ref(nav.some((item) => item.name === initialRoute) ? initialRoute : '总览');
const currentPageCopy = computed(
  () => pageCopy[route.value] ?? { eyebrow: '工作空间', description: '' },
);
const dark = ref(localStorage.getItem('pb-theme') === 'dark');
const mobile = ref(false);
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

function openVersionPopover(): void {
  versionPopover.value?.open();
}
</script>

<template>
  <AuthGate v-if="auth.loading || !auth.authenticated" />
  <div v-else class="app-shell">
    <div v-if="mobile" class="sidebar-mask" @click="mobile = false"></div>
    <aside class="sidebar" :class="{ visible: mobile }">
      <div class="brand">
        <a class="brand-home" href="#总览" aria-label="返回总览" @click="route = '总览'">
          <span class="brand-icon"><Box :size="25" /></span>
        </a>
        <span class="brand-copy">
          <a class="brand-title" href="#总览" @click="route = '总览'">PackBreaker</a>
          <VersionPopover ref="versionPopover" />
        </span>
      </div>
      <nav>
        <button
          v-for="item in nav"
          :key="item.name"
          :class="['nav-item', { active: route === item.name }]"
          @click="route = item.name"
        >
          <component :is="item.icon" :size="19" /><span>{{ item.name }}</span>
        </button>
      </nav>
    </aside>
    <div class="main-shell">
      <header class="topbar">
        <div class="breadcrumb">
          <button class="icon-button mobile-only" aria-label="展开导航" @click="mobile = true">
            <Menu :size="20" /></button
          ><span>工作空间</span><ChevronRight :size="15" /><b>{{ route }}</b>
        </div>
        <div class="top-actions">
          <button
            class="icon-button"
            :aria-label="dark ? '切换浅色主题' : '切换深色主题'"
            @click="dark = !dark"
          >
            <Sun v-if="dark" :size="18" /><Moon v-else :size="18" />
          </button>
          <button
            class="icon-button notification"
            aria-label="待确认通知"
            @click="route = '预演与确认'"
          >
            <Bell :size="18" />
          </button>
          <div class="top-user">
            <span class="avatar">A</span>
            <div class="top-user-copy"><strong>管理员</strong><small>本地安全会话</small></div>
            <button class="icon-button" aria-label="退出登录" title="退出登录" @click="logout">
              <LogOut :size="17" />
            </button>
          </div>
        </div>
      </header>
      <main>
        <div v-if="route !== '总览'" class="page-heading page-hero">
          <div class="page-title-copy">
            <div class="page-eyebrow"><Sparkles :size="14" />{{ currentPageCopy.eyebrow }}</div>
            <h1>{{ route }}</h1>
            <p>{{ currentPageCopy.description }}</p>
          </div>
          <div class="page-context">
            <span><i class="dot"></i> 服务在线</span>
            <span>安全模式</span>
          </div>
        </div>
        <TaskDefinitionCenter v-if="route === '任务中心'" @navigate="route = $event" />
        <PreflightReviewCenter v-else-if="route === '预演与确认'" />
        <OperationalOverview
          v-else-if="route === '总览'"
          @navigate="route = $event"
          @open-version="openVersionPopover"
        />
        <Management
          v-show="!['任务中心', '预演与确认', '总览'].includes(route)"
          :page="route"
          @navigate="route = $event"
        />
        <footer><span>PackBreaker</span></footer>
      </main>
    </div>
  </div>
</template>
