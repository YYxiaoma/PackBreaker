<script setup lang="ts">
import { ref } from 'vue';

import AIAgentManagement from './AIAgentManagement.vue';
import BackupManagement from './BackupManagement.vue';
import DownloaderManagement from './DownloaderManagement.vue';
import NotificationManagement from './NotificationManagement.vue';
import OperationalLogs from './OperationalLogs.vue';
import SiteManagement from './SiteManagement.vue';

defineProps<{ page: string }>();

const settingTab = ref('通知');
</script>

<template>
  <OperationalLogs v-if="page === '日志'" />
  <DownloaderManagement v-else-if="page === '下载器'" />
  <SiteManagement v-else-if="page === '站点管理'" />
  <div v-else-if="page === '系统设置'" class="panel">
    <el-tabs v-model="settingTab">
      <el-tab-pane
        v-for="item in ['通知', 'AI 助手', '备份恢复']"
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
    <div v-else class="settings-content">
      <h3>备份与恢复</h3>
      <BackupManagement />
    </div>
  </div>
</template>
