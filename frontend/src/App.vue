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
  ShieldCheck,
  ScrollText,
  Settings,
  ArrowUpCircle,
  ChevronRight,
  Bell,
  Sun,
  Moon,
  Menu,
  LogOut,
} from '@lucide/vue';
import { ElMessage, ElMessageBox } from 'element-plus';
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
  { name: '清理与对账', icon: ShieldCheck, group: '系统' },
  { name: '日志', icon: ScrollText },
  { name: '系统设置', icon: Settings },
  { name: '升级中心', icon: ArrowUpCircle },
];
const initialRoute = location.hash.slice(1)
  ? decodeURIComponent(location.hash.slice(1))
  : '任务中心';
const route = ref(nav.some((item) => item.name === initialRoute) ? initialRoute : '任务中心');
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
          <button
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
            <Bell :size="18" />
          </button>
        </div>
      </header>
      <main>
        <div class="page-heading">
          <h1>{{ route }}</h1>
        </div>
        <TaskCenter v-if="route === '任务中心'" />
        <PreflightReviewCenter v-else-if="route === '预演与确认'" />
        <OperationalOverview v-else-if="route === '总览'" />
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
