<script setup lang="ts">
import AIAgentManagement from './AIAgentManagement.vue';
import BackupManagement from './BackupManagement.vue';
import CookieCloudManagement from './CookieCloudManagement.vue';
import DownloaderManagement from './DownloaderManagement.vue';
import NotificationManagement from './NotificationManagement.vue';
import OperationalLogs from './OperationalLogs.vue';
import SiteManagement from './SiteManagement.vue';

import type { Route } from '../navigation';

defineProps<{ page: Route }>();
</script>

<template>
  <section class="workspace-page">
    <CookieCloudManagement v-if="page === '配置中心/CookieCloud配置'" />
    <SiteManagement v-else-if="page === '配置中心/站点配置'" />
    <DownloaderManagement v-else-if="page === '配置中心/下载器配置'" />

    <NotificationManagement v-else-if="page === '系统管理/通知配置'" />
    <OperationalLogs v-else-if="page === '系统管理/日志浏览'" />
    <AIAgentManagement v-else-if="page === '系统管理/Agent配置'" />
    <BackupManagement v-else-if="page === '系统管理/备份恢复'" />

    <div v-else class="workspace-route-fallback">
      <h2>页面不存在</h2>
      <p>当前页面路由无对应业务组件。</p>
    </div>
  </section>
</template>

<style scoped>
.workspace-page {
  width: 100%;
  min-width: 0;
}

.workspace-route-fallback {
  padding: 32px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface);
}

.workspace-route-fallback h2 {
  margin: 0 0 8px;
}

.workspace-route-fallback p {
  margin: 0;
  color: var(--muted);
}
</style>
