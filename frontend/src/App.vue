<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import {
  LayoutDashboard,
  ListChecks,
  Globe,
  Settings,
  Info,
  ChevronRight,
  ChevronDown,
  Bell,
  Search,
  Menu,
  FolderSearch,
  Copy,
  Cloud,
  HardDrive,
  Settings2,
} from '@lucide/vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import TaskCenterV2 from './components/TaskCenterV2.vue';
import OverviewDashboard from './components/OverviewDashboard.vue';
import WorkspaceManagement from './components/WorkspaceManagement.vue';
import MovieDedupPanel from './components/MovieDedupPanel.vue';
import AuthGate from './components/AuthGate.vue';
import PasswordChangeGate from './components/PasswordChangeGate.vue';
import UserMenuDrawer from './components/UserMenuDrawer.vue';
import NotificationInboxDrawer from './components/NotificationInboxDrawer.vue';
import VersionPopover from './components/VersionPopover.vue';
import packBreakerIcon from './assets/packbreaker-icon.png';
import AboutPage from './components/AboutPage.vue';
import { AUTH_REQUIRED_EVENT } from './api/client';
import { getAdminInboxUnreadCount } from './api/notifications';
import {
  ensurePrimaryExpanded,
  getPrimaryRoute,
  NAVIGATION_GROUPS,
  normalizeRoute,
  togglePrimaryExpansion,
  type PrimaryRoute,
  type Route,
  type SecondaryRoute,
} from './navigation';
import { useAuthStore } from './stores/auth';

type ThemeMode = 'light' | 'dark' | 'system';

const auth = useAuthStore();
const versionPopover = ref<InstanceType<typeof VersionPopover> | null>(null);
const userDrawerVisible = ref(false);
const notificationDrawerVisible = ref(false);
const unreadCount = ref(0);
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

type IconComponent = typeof LayoutDashboard;
type NavChild = { name: SecondaryRoute; icon: IconComponent };
type NavGroup = {
  name: PrimaryRoute;
  icon: IconComponent;
  children: NavChild[];
};

const primaryIconMap: Record<PrimaryRoute, IconComponent> = {
  总览: LayoutDashboard,
  任务中心: ListChecks,
  配置中心: Settings2,
  系统管理: Settings,
};

const childIconMap: Record<SecondaryRoute, IconComponent> = {
  数据拆包: FolderSearch,
  数据去重: Copy,
  CookieCloud配置: Cloud,
  站点配置: Globe,
  下载器配置: HardDrive,
  通知配置: Bell,
  日志浏览: ListChecks,
  Agent配置: Settings2,
  备份恢复: HardDrive,
};

const nav: NavGroup[] = NAVIGATION_GROUPS.map((group) => ({
  name: group.name,
  icon: primaryIconMap[group.name],
  children: group.children.map((name) => ({ name, icon: childIconMap[name] })),
}));

type SearchEntry = {
  name: string;
  target: Route;
  icon: IconComponent;
  aliases: string;
};

const searchableNav: SearchEntry[] = [
  { name: '总览', target: '总览', icon: LayoutDashboard, aliases: '仪表盘 dashboard 状态' },
  ...nav.flatMap((group) =>
    group.children.map((child) => ({
      name: child.name,
      target: normalizeRoute(child.name),
      icon: child.icon,
      aliases:
        group.name +
        ' ' +
        (
          {
            数据拆包: '任务 手动 监控 执行 重试',
            数据去重: '任务 去重 影片 文件',
            CookieCloud配置: 'cookie cloud 同步',
            站点配置: '站点 pt cookie api',
            下载器配置: '下载器 qb transmission',
            通知配置: '通知 telegram serverchan',
            日志浏览: '日志 记录 查询 log',
            Agent配置: 'agent ai 模型',
            备份恢复: '备份 恢复 导入 导出',
          } satisfies Record<SecondaryRoute, string>
        )[child.name],
    })),
  ),
  { name: '关于', target: '关于', icon: Info, aliases: '版本 帮助' },
];

const searchResults = computed(() => {
  const query = searchQuery.value.trim().toLocaleLowerCase();
  return searchableNav.filter(
    (item) => !query || (item.name + ' ' + item.aliases).toLocaleLowerCase().includes(query),
  );
});

function readHashRoute(): Route {
  const raw = location.hash.slice(1);
  if (!raw) return '总览';
  try {
    return normalizeRoute(decodeURIComponent(raw));
  } catch {
    return '总览';
  }
}

const route = ref<Route>(readHashRoute());
const initialPrimary = getPrimaryRoute(route.value);
const expandedPrimary = ref<PrimaryRoute[]>(initialPrimary ? [initialPrimary] : []);
const mobile = ref(false);

const breadcrumbItems = computed(() => {
  const primary = getPrimaryRoute(route.value);
  return primary && primary !== route.value
    ? [primary, route.value.split('/').at(-1) ?? route.value]
    : [route.value];
});

function selectPrimaryRoute(name: PrimaryRoute): void {
  const group = nav.find((item) => item.name === name);
  if (!group || group.children.length === 0) {
    route.value = name;
    expandedPrimary.value = [];
    return;
  }

  expandedPrimary.value = togglePrimaryExpansion(expandedPrimary.value, name);
}

function selectRoute(target: string): void {
  route.value = normalizeRoute(target);
}

watch(
  route,
  (v) => {
    expandedPrimary.value = ensurePrimaryExpanded(expandedPrimary.value, getPrimaryRoute(v));
    location.hash = encodeURIComponent(v);
    document.title = 'PackBreaker · ' + v;
    mobile.value = false;
  },
  { immediate: true },
);

window.addEventListener('hashchange', () => {
  route.value = readHashRoute();
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
      notificationDrawerVisible.value = false;
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
function selectSearchRoute(target: Route): void {
  selectRoute(target);
  searchQuery.value = '';
  searchOpen.value = false;
}
function searchOnEnter(): void {
  const first = searchResults.value[0];
  if (first) selectSearchRoute(first.target);
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
        <template v-for="item in nav" :key="item.name">
          <button
            type="button"
            :class="['nav-item nav-primary', { active: getPrimaryRoute(route) === item.name }]"
            :aria-current="getPrimaryRoute(route) === item.name ? 'page' : undefined"
            :aria-expanded="item.children.length ? expandedPrimary.includes(item.name) : undefined"
            @click="selectPrimaryRoute(item.name)"
          >
            <component :is="item.icon" :size="19" />
            <span>{{ item.name }}</span>
            <ChevronDown
              v-if="item.children.length"
              :size="16"
              class="nav-expand-icon"
              :class="{ expanded: expandedPrimary.includes(item.name) }"
              aria-hidden="true"
            />
          </button>
          <div
            v-if="item.children.length && expandedPrimary.includes(item.name)"
            class="nav-children"
          >
            <button
              v-for="child in item.children"
              :key="child.name"
              type="button"
              :class="['nav-subitem', { active: normalizeRoute(child.name) === route }]"
              :aria-current="normalizeRoute(child.name) === route ? 'page' : undefined"
              @click="selectRoute(child.name)"
            >
              <component :is="child.icon" :size="17" />
              <span>{{ child.name }}</span>
            </button>
          </div>
        </template>
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
          ><span>工作空间</span>
          <template v-for="item in breadcrumbItems" :key="item">
            <ChevronRight :size="15" />
            <b>{{ item }}</b>
          </template>
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
              :aria-selected="route === item.target"
              @mousedown.prevent="selectSearchRoute(item.target)"
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
            class="header-bell"
            aria-label="查看通知"
            @click="notificationDrawerVisible = true"
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
        <OverviewDashboard v-if="route === '总览'" @navigate="selectRoute($event)" />
        <AboutPage v-else-if="route === '关于'" @open-version="openVersionPopover" />
        <TaskCenterV2 v-else-if="route === '任务中心/数据拆包'" />
        <MovieDedupPanel v-else-if="route === '任务中心/数据去重'" />
        <WorkspaceManagement v-else :page="route" />
        <footer><span>PackBreaker</span></footer>
      </main>
    </div>
    <button
      type="button"
      class="about-corner-button"
      :class="{ active: route === '关于' }"
      aria-label="关于 PackBreaker"
      :aria-current="route === '关于' ? 'page' : undefined"
      @click="route = '关于'"
    >
      <Info :size="17" aria-hidden="true" />
      <span>关于</span>
    </button>
    <UserMenuDrawer
      v-model="userDrawerVisible"
      :username="auth.username || 'admin'"
      :theme-mode="themeMode"
      @update:theme-mode="themeMode = $event"
      @logout="logout"
    />
    <NotificationInboxDrawer
      v-model="notificationDrawerVisible"
      @unread-change="unreadCount = $event"
    />
  </div>
</template>
