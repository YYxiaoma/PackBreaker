<script setup lang="ts">
import { ref } from 'vue';
import { Cloud, Globe } from '@lucide/vue';

import AIAgentManagement from './AIAgentManagement.vue';
import BackupManagement from './BackupManagement.vue';
import CookieCloudManagement from './CookieCloudManagement.vue';
import DownloaderManagement from './DownloaderManagement.vue';
import NotificationManagement from './NotificationManagement.vue';
import OperationalLogs from './OperationalLogs.vue';
import SiteManagement from './SiteManagement.vue';

defineProps<{ page: string }>();

type SiteTab = 'sites' | 'cookiecloud';

const siteTab = ref<SiteTab>('sites');
const settingTab = ref('通知');
</script>

<template>
  <section v-if="page === '站点管理'" class="site-workspace">
    <div class="site-mode-cards" aria-label="站点管理视图">
      <button
        type="button"
        class="site-mode-card"
        :class="{ active: siteTab === 'sites' }"
        :aria-pressed="siteTab === 'sites'"
        aria-controls="site-config-panel"
        @click="siteTab = 'sites'"
      >
        <span class="site-mode-icon"><Globe :size="22" /></span>
        <span class="site-mode-copy">
          <b>站点配置</b>
          <small>站点管理、凭证与连接测试</small>
        </span>
      </button>
      <button
        type="button"
        class="site-mode-card"
        :class="{ active: siteTab === 'cookiecloud' }"
        :aria-pressed="siteTab === 'cookiecloud'"
        aria-controls="site-cookiecloud-panel"
        @click="siteTab = 'cookiecloud'"
      >
        <span class="site-mode-icon"><Cloud :size="22" /></span>
        <span class="site-mode-copy">
          <b>CookieCloud 同步</b>
          <small>CookieCloud 连接、同步与计划配置</small>
        </span>
      </button>
    </div>
    <div v-if="siteTab === 'sites'" id="site-config-panel" class="site-mode-content">
      <SiteManagement />
    </div>
    <div v-else id="site-cookiecloud-panel" class="site-mode-content">
      <CookieCloudManagement />
    </div>
  </section>
  <div v-else-if="page === '系统设置'" class="panel system-settings-panel">
    <el-tabs v-model="settingTab">
      <el-tab-pane
        v-for="item in ['通知', '下载器', '日志', 'AI 助手', '备份恢复']"
        :key="item"
        :name="item"
        :label="item"
      />
    </el-tabs>
    <div v-if="settingTab === '通知'" class="settings-content">
      <NotificationManagement />
    </div>
    <div v-else-if="settingTab === 'AI 助手'" class="settings-content">
      <AIAgentManagement />
    </div>
    <div v-else-if="settingTab === '下载器'" class="settings-content">
      <DownloaderManagement />
    </div>
    <div v-else-if="settingTab === '日志'" class="settings-content">
      <OperationalLogs />
    </div>
    <div v-else class="settings-content">
      <h3>备份与恢复</h3>
      <BackupManagement />
    </div>
  </div>
</template>

<style scoped>
.site-workspace,
.site-mode-content,
.system-settings-panel,
.system-settings-panel .el-tabs,
.system-settings-panel .settings-content {
  width: 100%;
  min-width: 0;
}

.site-workspace {
  display: grid;
  gap: 14px;
}

.site-mode-cards {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.site-mode-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 18px;
  min-width: 0;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface);
  color: inherit;
  text-align: left;
  cursor: pointer;
  transition: 0.16s ease;
}

.site-mode-card:hover {
  border-color: var(--blue);
  box-shadow: 0 8px 26px rgba(31, 93, 255, 0.08);
}

.site-mode-card.active {
  border-color: var(--blue);
  background: var(--blue-soft);
  box-shadow: 0 0 0 2px rgba(31, 93, 255, 0.12);
}

.site-mode-card:focus-visible {
  outline: 2px solid var(--blue);
  outline-offset: 2px;
}

.site-mode-icon {
  display: grid;
  place-items: center;
  flex: 0 0 42px;
  width: 42px;
  height: 42px;
  border-radius: 11px;
  background: var(--blue-soft);
  color: var(--blue);
}

.site-mode-card.active .site-mode-icon {
  background: var(--blue);
  color: #fff;
}

.site-mode-copy {
  display: grid;
  min-width: 0;
  gap: 4px;
}

.site-mode-copy small {
  color: var(--muted);
}

@media (max-width: 640px) {
  .site-mode-cards {
    grid-template-columns: 1fr;
  }
}
</style>
