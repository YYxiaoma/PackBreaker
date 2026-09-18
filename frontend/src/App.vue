<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import {
  Box,
  LayoutDashboard,
  ListChecks,
  GitBranch,
  Globe,
  HardDrive,
  ShieldCheck,
  ScrollText,
  Settings,
  Info,
  ChevronRight,
  Menu,
  Sparkles,
} from '@lucide/vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import TaskDefinitionCenter from './components/TaskDefinitionCenter.vue';
import PreflightReviewCenter from './components/PreflightReviewCenter.vue';
import OperationalOverview from './components/OperationalOverview.vue';
import Management from './components/Management.vue';
import AuthGate from './components/AuthGate.vue';
import PasswordChangeGate from './components/PasswordChangeGate.vue';
import UserMenuDrawer from './components/UserMenuDrawer.vue';
import VersionPopover from './components/VersionPopover.vue';
import AboutPage from './components/AboutPage.vue';
import { AUTH_REQUIRED_EVENT } from './api/client';
import { getAdminInboxUnreadCount } from './api/notifications';
import { useAuthStore } from './stores/auth';

type ThemeMode = 'light' | 'dark' | 'system';

const auth = useAuthStore();
const versionPopover = ref<InstanceType<typeof VersionPopover> | null>(null);
const userDrawerVisible = ref(false);
const unreadCount = ref(0);
const systemDark = ref(window.matchMedia('(prefers-color-scheme: dark)').matches);
const storedTheme = localStorage.getItem('pb-theme-mode');
const legacyTheme = localStorage.getItem('pb-theme');
const themeMode = ref<ThemeMode>(
  storedTheme === 'light' || storedTheme === 'dark' || storedTheme === 'system'
    ? storedTheme
    : legacyTheme === 'dark' || legacyTheme === 'light'
      ? legacyTheme
      : 'system',
);
const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
const dark = computed(
  () => themeMode.value === 'dark' || (themeMode.value === 'system' && systemDark.value),
);
const avatarInitial = computed(() => auth.username?.trim().charAt(0).toUpperCase() || 'A');
let unreadTimer: ReturnType<typeof setInterval> | undefined;

function handleAuthRequired(): void {
  auth.markUnauthenticated();
}

onMounted(() => {
  window.addEventListener(AUTH_REQUIRED_EVENT, handleAuthRequired);
  mediaQuery.addEventListener('change', handleSystemThemeChange);
  void auth.bootstrap();
});
onUnmounted(() => {
  window.removeEventListener(AUTH_REQUIRED_EVENT, handleAuthRequired);
  mediaQuery.removeEventListener('change', handleSystemThemeChange);
  if (unreadTimer) clearInterval(unreadTimer);
});

function handleSystemThemeChange(event: MediaQueryListEvent): void {
  systemDark.value = event.matches;
}

const nav = [
  { name: '总览', icon: LayoutDashboard },
  { name: '任务中心', icon: ListChecks },
  { name: '预演与确认', icon: GitBranch },
  { name: '站点管理', icon: Globe },
  { name: '下载器', icon: HardDrive },
  { name: '清理与对账', icon: ShieldCheck },
  { name: '日志', icon: ScrollText },
  { name: '系统设置', icon: Settings },
  { name: '关于', icon: Info },
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
    description: '管理通知、AI 助手与数据库备份恢复策略。',
  },
  关于: {
    eyebrow: '项目信息',
    description: '查看版本、项目定位、安全原则、许可证与更新入口。',
  },
};
const initialRoute = location.hash.slice(1) ? decodeURIComponent(location.hash.slice(1)) : '总览';
const route = ref(nav.some((item) => item.name === initialRoute) ? initialRoute : '总览');
const currentPageCopy = computed(
  () => pageCopy[route.value] ?? { eyebrow: '工作空间', description: '' },
);
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
  },
  { immediate: true },
);
watch(
  themeMode,
  (value) => {
    localStorage.setItem('pb-theme-mode', value);
    localStorage.removeItem('pb-theme');
  },
  { immediate: true },
);
watch(
  () => auth.authenticated && !auth.mustChangePassword,
  (ready) => {
    if (unreadTimer) {
      clearInterval(unreadTimer);
      unreadTimer = undefined;
    }
    if (!ready) {
      unreadCount.value = 0;
      userDrawerVisible.value = false;
      return;
    }
    void refreshUnreadCount();
    unreadTimer = setInterval(() => void refreshUnreadCount(), 30_000);
  },
  { immediate: true },
);

async function refreshUnreadCount(): Promise<void> {
  try {
    unreadCount.value = await getAdminInboxUnreadCount();
  } catch {
    // 顶栏未读红点读取失败不能阻断主界面。
  }
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

function openVersionPopover(): void {
  versionPopover.value?.open();
}
</script>

<template>
  <AuthGate v-if="auth.loading || !auth.authenticated" />
  <PasswordChangeGate v-else-if="auth.mustChangePassword" />
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
            class="top-user user-menu-trigger"
            aria-label="打开管理员菜单"
            @click="userDrawerVisible = true"
          >
            <el-badge :is-dot="unreadCount > 0" class="user-avatar-badge">
              <span class="avatar">{{ avatarInitial }}</span>
            </el-badge>
            <div class="top-user-copy">
              <strong>{{ auth.username || 'admin' }}</strong>
              <small>{{ unreadCount ? `${unreadCount} 条未读` : '本地安全会话' }}</small>
            </div>
          </button>
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
        <AboutPage v-else-if="route === '关于'" @open-version="openVersionPopover" />
        <Management
          v-show="!['任务中心', '预演与确认', '总览', '关于'].includes(route)"
          :page="route"
          @navigate="route = $event"
        />
        <footer><span>PackBreaker</span></footer>
      </main>
    </div>
    <UserMenuDrawer
      v-model="userDrawerVisible"
      :username="auth.username || 'admin'"
      :theme-mode="themeMode"
      @update:theme-mode="themeMode = $event"
      @unread-change="unreadCount = $event"
      @logout="logout"
    />
  </div>
</template>
