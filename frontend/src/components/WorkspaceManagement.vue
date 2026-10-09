<script setup lang="ts">
import { ref } from 'vue';

import AIAgentManagement from './AIAgentManagement.vue';
import BackupManagement from './BackupManagement.vue';
import CookieCloudManagement from './CookieCloudManagement.vue';
import DownloaderManagement from './DownloaderManagement.vue';
import NotificationManagement from './NotificationManagement.vue';
import OperationalLogs from './OperationalLogs.vue';
import SiteManagement from './SiteManagement.vue';

defineProps<{ page: string }>();

const settingTab = ref('通知');
</script>

<template>
  <SiteManagement v-if="page === '站点管理'" />
  <div v-else-if="page === '系统设置'" class="panel system-settings-panel">
    <el-tabs v-model="settingTab">
      <el-tab-pane
        v-for="item in ['通知', 'CookieCloud', '下载器', '日志', 'AI 助手', '备份恢复']"
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
    <div v-else-if="settingTab === 'CookieCloud'" class="settings-content">
      <CookieCloudManagement />
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
.system-settings-panel,
.system-settings-panel .el-tabs,
.system-settings-panel .settings-content {
  width: 100%;
  min-width: 0;
}
</style>
