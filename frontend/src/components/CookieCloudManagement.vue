<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { Cloud, RefreshCw, ShieldCheck } from '@lucide/vue';
import { ElMessage } from 'element-plus';

import { ApiProblem } from '../api/client';
import {
  getCookieCloudSettings,
  syncCookieCloud,
  testCookieCloud,
  updateCookieCloudSettings,
  type CookieCloudSettings,
} from '../api/cookiecloud';

interface Draft {
  enabled: boolean;
  serverUrl: string;
  uuid: string;
  password: string;
  clearPassword: boolean;
  autoSync: boolean;
  syncIntervalMinutes: number;
  requestTimeoutSeconds: number;
}

const settings = ref<CookieCloudSettings>();
const loading = ref(false);
const saving = ref(false);
const probing = ref(false);
const syncing = ref(false);
const draft = reactive<Draft>({
  enabled: false,
  serverUrl: '',
  uuid: '',
  password: '',
  clearPassword: false,
  autoSync: true,
  syncIntervalMinutes: 30,
  requestTimeoutSeconds: 15,
});

const connectionType = computed(() => {
  if (settings.value?.connection_status === 'OK') return 'success';
  if (settings.value?.connection_status === 'FAILED') return 'danger';
  return 'info';
});
const connectionText = computed(() => {
  if (settings.value?.connection_status === 'OK') return '连接与解密正常';
  if (settings.value?.connection_status === 'FAILED') return '上次测试或同步失败';
  return '尚未测试';
});
const syncType = computed(() => {
  if (settings.value?.last_sync_status === 'SUCCESS') return 'success';
  if (settings.value?.last_sync_status === 'FAILED') return 'danger';
  return 'info';
});
const syncText = computed(() => {
  if (settings.value?.last_sync_status === 'SUCCESS') return '上次同步成功';
  if (settings.value?.last_sync_status === 'FAILED') {
    return '上次同步失败 · ' + (settings.value.last_sync_error_code ?? 'UNKNOWN');
  }
  return '尚未同步';
});
const insecureHttp = computed(() => draft.serverUrl.trim().toLowerCase().startsWith('http://'));

function applySettings(value: CookieCloudSettings): void {
  settings.value = value;
  Object.assign(draft, {
    enabled: value.enabled,
    serverUrl: value.server_url,
    uuid: value.uuid,
    password: '',
    clearPassword: false,
    autoSync: value.auto_sync,
    syncIntervalMinutes: value.sync_interval_minutes,
    requestTimeoutSeconds: value.request_timeout_seconds,
  });
}

function errorText(caught: unknown, fallback: string): string {
  return caught instanceof ApiProblem ? caught.message : fallback;
}

function formatTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function validate(): boolean {
  if (draft.enabled && !draft.serverUrl.trim()) {
    ElMessage.warning('启用 CookieCloud 前请输入服务器地址');
    return false;
  }
  if (draft.enabled && !draft.uuid.trim()) {
    ElMessage.warning('启用 CookieCloud 前请输入 UUID');
    return false;
  }
  if (
    draft.enabled &&
    !draft.password &&
    (!settings.value?.password_configured || draft.clearPassword)
  ) {
    ElMessage.warning('启用 CookieCloud 前必须配置密码');
    return false;
  }
  return true;
}

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    applySettings(await getCookieCloudSettings());
  } catch (caught) {
    ElMessage.error(errorText(caught, 'CookieCloud 配置读取失败'));
  } finally {
    loading.value = false;
  }
}

async function save(): Promise<void> {
  if (!settings.value || !validate()) return;
  saving.value = true;
  try {
    const passwordAction = draft.clearPassword ? 'CLEAR' : draft.password ? 'SET' : 'KEEP';
    const updated = await updateCookieCloudSettings(settings.value, {
      enabled: draft.enabled,
      server_url: draft.serverUrl.trim(),
      uuid: draft.uuid.trim(),
      password_action: passwordAction,
      ...(passwordAction === 'SET' ? { password: draft.password } : {}),
      auto_sync: draft.autoSync,
      sync_interval_minutes: draft.syncIntervalMinutes,
      request_timeout_seconds: draft.requestTimeoutSeconds,
    });
    applySettings(updated);
    ElMessage.success('CookieCloud 配置已保存');
  } catch (caught) {
    ElMessage.error(errorText(caught, 'CookieCloud 配置保存失败'));
  } finally {
    saving.value = false;
  }
}

async function probe(): Promise<void> {
  probing.value = true;
  try {
    const result = await testCookieCloud();
    await refresh();
    ElMessage.success(
      '连接与解密正常：' + result.domain_count + ' 个域名，' + result.cookie_count + ' 条 Cookie',
    );
  } catch (caught) {
    await refresh();
    ElMessage.error(errorText(caught, 'CookieCloud 测试失败'));
  } finally {
    probing.value = false;
  }
}

async function syncNow(): Promise<void> {
  syncing.value = true;
  try {
    const result = await syncCookieCloud();
    await refresh();
    ElMessage.success(
      '同步完成：匹配 ' +
        result.matched_sites +
        ' 个站点，更新 ' +
        result.updated_sites +
        ' 个站点',
    );
  } catch (caught) {
    await refresh();
    ElMessage.error(errorText(caught, 'CookieCloud 同步失败'));
  } finally {
    syncing.value = false;
  }
}

onMounted(() => void refresh());
</script>

<template>
  <div v-loading="loading" class="cookiecloud-management">
    <div class="settings-section-heading">
      <div>
        <h3>CookieCloud</h3>
        <p>
          对接 easychen/CookieCloud。PackBreaker 只向服务器发送 UUID，密码仅在本机 SecretStore
          中用于解密。
        </p>
      </div>
      <Cloud :size="24" aria-hidden="true" />
    </div>

    <div v-if="settings" class="cookiecloud-status-grid">
      <div class="cookiecloud-status-item">
        <span>连接状态</span>
        <el-tag :type="connectionType" effect="plain">{{ connectionText }}</el-tag>
        <small>最后测试：{{ formatTime(settings.last_test_at) }}</small>
      </div>
      <div class="cookiecloud-status-item">
        <span>同步状态</span>
        <el-tag :type="syncType" effect="plain">{{ syncText }}</el-tag>
        <small>最后同步：{{ formatTime(settings.last_sync_at) }}</small>
      </div>
      <div class="cookiecloud-status-item">
        <span>最近结果</span>
        <strong>{{ settings.updated_sites }} / {{ settings.matched_sites }}</strong>
        <small>更新 / 匹配站点 · 未匹配域名 {{ settings.unmatched_domains }}</small>
      </div>
    </div>

    <el-alert
      v-if="insecureHttp"
      type="warning"
      :closable="false"
      title="当前使用 HTTP。仅建议在受信任的内网或反向代理后使用；公网 CookieCloud 请优先使用 HTTPS。"
      class="cookiecloud-alert"
    />

    <el-form label-position="top" class="cookiecloud-form">
      <el-form-item label="启用 CookieCloud">
        <el-switch v-model="draft.enabled" />
      </el-form-item>
      <div class="cookiecloud-form-grid">
        <el-form-item label="服务器地址">
          <el-input
            v-model="draft.serverUrl"
            placeholder="https://cookie.example.com 或 https://host/api"
          />
        </el-form-item>
        <el-form-item label="UUID">
          <el-input v-model="draft.uuid" autocomplete="off" placeholder="CookieCloud UUID" />
        </el-form-item>
      </div>
      <el-form-item label="密码">
        <el-input
          v-model="draft.password"
          type="password"
          show-password
          autocomplete="new-password"
          :placeholder="settings?.password_configured ? '已配置；留空保持不变' : 'CookieCloud 密码'"
        />
        <el-checkbox
          v-if="settings?.password_configured"
          v-model="draft.clearPassword"
          :disabled="Boolean(draft.password)"
          >清除已保存密码</el-checkbox
        >
      </el-form-item>

      <div class="cookiecloud-form-grid">
        <el-form-item label="自动同步">
          <el-switch v-model="draft.autoSync" />
        </el-form-item>
        <el-form-item label="同步间隔（分钟）">
          <el-input-number
            v-model="draft.syncIntervalMinutes"
            :min="5"
            :max="10080"
            :disabled="!draft.autoSync"
          />
        </el-form-item>
        <el-form-item label="请求超时（秒）">
          <el-input-number v-model="draft.requestTimeoutSeconds" :min="1" :max="120" />
        </el-form-item>
      </div>
    </el-form>

    <div class="cookiecloud-actions">
      <el-button :loading="probing" :disabled="!settings?.password_configured" @click="probe">
        <ShieldCheck :size="16" /> 测试连接
      </el-button>
      <el-button :loading="syncing" :disabled="!settings?.password_configured" @click="syncNow">
        <RefreshCw :size="16" /> 立即同步
      </el-button>
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
    </div>
  </div>
</template>

<style scoped>
.cookiecloud-management {
  display: grid;
  gap: 20px;
}

.settings-section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.settings-section-heading h3 {
  margin: 0 0 6px;
}

.settings-section-heading p {
  margin: 0;
  color: var(--el-text-color-secondary);
  line-height: 1.65;
}

.cookiecloud-status-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.cookiecloud-status-item {
  display: grid;
  gap: 8px;
  padding: 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
}

.cookiecloud-status-item span,
.cookiecloud-status-item small {
  color: var(--el-text-color-secondary);
}

.cookiecloud-status-item strong {
  font-size: 20px;
}

.cookiecloud-form-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}

.cookiecloud-alert {
  margin: 0;
}

.cookiecloud-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

@media (max-width: 900px) {
  .cookiecloud-status-grid,
  .cookiecloud-form-grid {
    grid-template-columns: 1fr;
  }

  .cookiecloud-actions {
    flex-wrap: wrap;
  }
}
</style>
