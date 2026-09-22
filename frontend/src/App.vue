<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import {
  LayoutDashboard,
  ListChecks,
  Globe,
  HardDrive,
  ScrollText,
  Settings,
  Info,
  ChevronRight,
  ChevronDown,
  Bell,
  Search,
  CheckCircle2,
  CircleAlert,
  Menu,
} from '@lucide/vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import TaskDefinitionCenter from './components/TaskDefinitionCenter.vue';
import OverviewDashboard from './components/OverviewDashboard.vue';
import WorkspaceManagement from './components/WorkspaceManagement.vue';
import AuthGate from './components/AuthGate.vue';
import PasswordChangeGate from './components/PasswordChangeGate.vue';
import UserMenuDrawer from './components/UserMenuDrawer.vue';
import VersionPopover from './components/VersionPopover.vue';
import packBreakerIcon from './assets/packbreaker-icon.png';
import AboutPage from './components/AboutPage.vue';
import { AUTH_REQUIRED_EVENT } from './api/client';
import { getAdminInboxUnreadCount } from './api/notifications';
import { getSystemHealth, type SystemHealth } from './api/system';
import { normalizePrimaryRoute, type PrimaryRoute } from './navigation';
import { useAuthStore } from './stores/auth';

type ThemeMode = 'light' | 'dark' | 'system';

const auth = useAuthStore();
const versionPopover = ref<InstanceType<typeof VersionPopover> | null>(null);
const userDrawerVisible = ref(false);
const unreadCount = ref(0);
const headerHealth = ref<SystemHealth | null>(null);
const headerHealthError = ref(false);
const searchQuery = ref('');
const searchOpen = ref(false);
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
let healthTimer: ReturnType<typeof setInterval> | undefined;

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
  if (healthTimer) clearInterval(healthTimer);
});

function handleSystemThemeChange(event: MediaQueryListEvent): void {
  systemDark.value = event.matches;
}

const nav: Array<{ name: PrimaryRoute; icon: typeof LayoutDashboard }> = [
  { name: '总览', icon: LayoutDashboard },
  { name: '任务中心', icon: ListChecks },
  { name: '站点管理', icon: Globe },
  { name: '下载器', icon: HardDrive },
  { name: '日志', icon: ScrollText },
  { name: '系统设置', icon: Settings },
  { name: '关于', icon: Info },
];
const searchResults = computed(() => {
  const query = searchQuery.value.trim().toLocaleLowerCase();
  const aliases: Partial<Record<PrimaryRoute, string>> = {
    总览: '仪表盘 dashboard 状态',
    任务中心: '任务 审批 执行 计划 重试',
    站点管理: '站点 pt cookie api',
    下载器: '下载 qb transmission',
    日志: '日志 记录 查询 log',
    系统设置: '通知 备份 ai 设置',
    关于: '版本 帮助',
  };
  return nav.filter(
    (item) =>
      !query || (item.name + ' ' + (aliases[item.name] ?? '')).toLocaleLowerCase().includes(query),
  );
});
const healthLabel = computed(() =>
  !headerHealth.value || headerHealthError.value
    ? '状态暂不可用'
    : headerHealth.value.status === 'ok'
      ? '系统运行正常'
      : headerHealth.value.status === 'warning'
        ? '系统需要关注'
        : '系统运行异常',
);
const healthLevel = computed(() =>
  !headerHealth.value || headerHealthError.value ? 'unknown' : headerHealth.value.status,
);
const initialRoute = location.hash.slice(1) ? decodeURIComponent(location.hash.slice(1)) : '总览';
const route = ref<PrimaryRoute>(normalizePrimaryRoute(initialRoute));
const mobile = ref(false);
watch(route, (v) => {
  location.hash = encodeURIComponent(v);
  document.title = `PackBreaker · ${v}`;
  mobile.value = false;
});
window.addEventListener('hashchange', () => {
  const next = decodeURIComponent(location.hash.slice(1));
  route.value = normalizePrimaryRoute(next);
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
    if (healthTimer) {
      clearInterval(healthTimer);
      healthTimer = undefined;
    }
    if (!ready) {
      unreadCount.value = 0;
      headerHealth.value = null;
      headerHealthError.value = false;
      userDrawerVisible.value = false;
      return;
    }
    void refreshUnreadCount();
    void refreshHeaderHealth();
    unreadTimer = setInterval(() => void refreshUnreadCount(), 30_000);
    healthTimer = setInterval(() => void refreshHeaderHealth(), 60_000);
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
async function refreshHeaderHealth(): Promise<void> {
  try {
    headerHealth.value = await getSystemHealth();
    headerHealthError.value = false;
  } catch {
    headerHealthError.value = true;
  }
}
function selectSearchRoute(target: PrimaryRoute): void {
  route.value = target;
  searchQuery.value = '';
  searchOpen.value = false;
}
function searchOnEnter(): void {
  const first = searchResults.value[0];
  if (first) selectSearchRoute(first.name);
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
          <span class="brand-icon"
            ><img :src="packBreakerIcon" alt="" width="36" height="36"
          /></span>
        </a>
        <span class="brand-copy">
          <a class="brand-title" href="#总览" @click="route = '总览'">PackBreaker</a>
          <VersionPopover ref="versionPopover" />
        </span>
      </div>
      <nav aria-label="主导航">
        <button
          v-for="item in nav"
          :key="item.name"
          :class="['nav-item', { active: route === item.name }]"
          @click="route = item.name"
        >
          <component :is="item.icon" :size="19" /><span>{{ item.name }}</span>
        </button>
      </nav>
      <div class="sidebar-slogan">
        <strong>更自动，更自由</strong>
        <small>PACK MORE POSSIBILITIES</small>
        <span aria-hidden="true"></span>
      </div>
    </aside>
    <div class="main-shell">
      <header class="topbar">
        <div class="breadcrumb">
          <button class="icon-button mobile-only" aria-label="展开导航" @click="mobile = true">
            <Menu :size="20" /></button
          ><span>工作空间</span><ChevronRight :size="15" /><b>{{ route }}</b>
        </div>
        <div class="topbar-search" @focusin="searchOpen = true" @focusout="searchOpen = false">
          <Search :size="19" aria-hidden="true" />
          <input
            v-model="searchQuery"
            aria-label="搜索页面或功能"
            placeholder="搜索任务、站点或日志..."
            type="search"
            autocomplete="off"
            @keydown.enter.prevent="searchOnEnter"
            @keydown.esc="searchOpen = false"
          />
          <div
            v-if="searchOpen"
            class="topbar-search-results"
            role="listbox"
            aria-label="可前往的功能"
          >
            <button
              v-for="item in searchResults"
              :key="item.name"
              type="button"
              role="option"
              :aria-selected="route === item.name"
              @mousedown.prevent="selectSearchRoute(item.name)"
            >
              <component :is="item.icon" :size="17" /><span>{{ item.name }}</span>
              <ChevronRight :size="15" />
            </button>
            <span v-if="!searchResults.length" class="search-no-result">未找到对应功能</span>
          </div>
        </div>
        <div class="top-actions">
          <button
            type="button"
            class="header-health"
            :class="healthLevel"
            :title="headerHealthError ? '系统状态读取失败，请进入总览刷新' : '查看系统状态'"
            @click="route = '总览'"
          >
            <CheckCircle2 v-if="healthLevel === 'ok'" :size="15" />
            <CircleAlert v-else :size="15" />
            {{ healthLabel }}
          </button>
          <button
            type="button"
            class="header-bell"
            aria-label="查看通知"
            @click="userDrawerVisible = true"
          >
            <Bell :size="20" />
            <span v-if="unreadCount > 0" class="header-unread-dot"></span>
          </button>
          <button
            class="top-user user-menu-trigger"
            aria-label="打开管理员菜单"
            @click="userDrawerVisible = true"
          >
            <span class="avatar">{{ avatarInitial }}</span>
            <div class="top-user-copy">
              <strong>{{ auth.username || 'admin' }}</strong>
            </div>
            <ChevronDown :size="16" class="user-chevron" />
          </button>
        </div>
      </header>
      <main>
        <TaskDefinitionCenter
          v-if="route === '任务中心'"
          @navigate="route = normalizePrimaryRoute($event)"
        />
        <OverviewDashboard
          v-else-if="route === '总览'"
          @navigate="route = normalizePrimaryRoute($event)"
        />
        <AboutPage v-else-if="route === '关于'" @open-version="openVersionPopover" />
        <WorkspaceManagement v-show="!['任务中心', '总览', '关于'].includes(route)" :page="route" />
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
