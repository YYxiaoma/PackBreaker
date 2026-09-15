<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { storeToRefs } from 'pinia';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Activity, Globe, Plus, RefreshCw, Settings2 } from '@lucide/vue';

import { ApiProblem, toApiProblem } from '../api/client';
import { credentialKindForSite } from '../api/sites';
import type {
  Site,
  SiteCreateInput,
  SiteCredentialInput,
  SiteHealth,
  SiteKind,
  SitePatchInput,
} from '../api/sites';
import { useSiteStore } from '../stores/sites';

interface SiteDraft {
  id: string | null;
  originalType: SiteKind | null;
  name: string;
  type: SiteKind;
  baseUrl: string;
  credential: string;
  clearCredential: boolean;
  credentialConfigured: boolean;
}

const store = useSiteStore();
const { items, health, loading, error, busy } = storeToRefs(store);
const dialog = ref(false);
const saving = ref(false);
const draft = reactive<SiteDraft>({
  id: null,
  originalType: null,
  name: '',
  type: 'MTEAM',
  baseUrl: '',
  credential: '',
  clearCredential: false,
  credentialConfigured: false,
});

const editing = computed(() => draft.id !== null);

onMounted(() => {
  void refresh(false);
});

function kindLabel(kind: SiteKind): string {
  if (kind === 'MTEAM') return 'M-Team';
  if (kind === 'HDTIME') return 'HDTime';
  return 'HHClub';
}

function credentialLabel(kind: SiteKind): string {
  return kind === 'MTEAM' ? 'API Key' : 'Cookie';
}

function connectionLabel(status: Site['connection_status']): string {
  if (status === 'OK') return '已验证';
  if (status === 'FAILED') return '失败';
  return '未测试';
}

function statusTag(status: Site['connection_status']): 'success' | 'danger' | 'info' {
  if (status === 'OK') return 'success';
  if (status === 'FAILED') return 'danger';
  return 'info';
}

function circuitLabel(value: SiteHealth['circuit_state'] | undefined): string {
  if (value === 'OPEN') return '已打开';
  if (value === 'HALF_OPEN') return '半开探测';
  if (value === 'CLOSED') return '关闭';
  return '未知';
}

function circuitTag(
  value: SiteHealth['circuit_state'] | undefined,
): 'success' | 'danger' | 'warning' | 'info' {
  if (value === 'OPEN') return 'danger';
  if (value === 'HALF_OPEN') return 'warning';
  if (value === 'CLOSED') return 'success';
  return 'info';
}

function cacheHitRate(item: SiteHealth | undefined): string {
  if (!item) return '—';
  const total = item.cache_hits + item.cache_misses;
  if (!total) return '0%';
  return `${Math.round((item.cache_hits / total) * 100)}%`;
}

function capabilityBoolean(item: Site, key: string): string {
  const value = item.capabilities[key];
  if (typeof value !== 'boolean') return '—';
  return value ? '支持' : '不支持';
}

function isBusy(item: Site, operation: string): boolean {
  return busy.value[`${operation}:${item.id}`] === true;
}

function problemText(problem: ApiProblem): string {
  return problem.traceId ? `${problem.message} · trace_id ${problem.traceId}` : problem.message;
}

async function refresh(notify = true) {
  try {
    await store.refresh();
    if (notify) ElMessage.success('站点配置与可靠性状态已刷新');
  } catch (caught) {
    if (notify) ElMessage.error(problemText(toApiProblem(caught)));
  }
}

function resetDraft() {
  Object.assign(draft, {
    id: null,
    originalType: null,
    name: '',
    type: 'MTEAM' as SiteKind,
    baseUrl: '',
    credential: '',
    clearCredential: false,
    credentialConfigured: false,
  });
}

function openCreate() {
  resetDraft();
  dialog.value = true;
}

function openEdit(item: Site) {
  resetDraft();
  Object.assign(draft, {
    id: item.id,
    originalType: item.type,
    name: item.name,
    type: item.type,
    baseUrl: item.base_url,
    credentialConfigured: item.credential_configured,
  });
  dialog.value = true;
}

function credentialPayload(): SiteCredentialInput | undefined {
  if (draft.clearCredential || !draft.credential) return undefined;
  return {
    kind: credentialKindForSite(draft.type),
    value: draft.credential,
  };
}

function validateDraft(): boolean {
  if (!draft.name.trim()) {
    ElMessage.warning('请输入站点名称');
    return false;
  }
  if (!/^https:\/\/[^/?#]+\/?$/i.test(draft.baseUrl.trim())) {
    ElMessage.warning('站点地址必须是无凭证、无路径、无查询参数的 HTTPS origin');
    return false;
  }
  if (/\r|\n|\0/.test(draft.credential)) {
    ElMessage.warning('站点凭证不能包含换行或 NUL');
    return false;
  }
  return true;
}

async function handleWriteProblem(caught: unknown) {
  const problem = toApiProblem(caught);
  if (problem.status === 412) {
    await store.refresh().catch(() => undefined);
    ElMessage.warning('站点配置已被其他请求更新，已刷新，请重新打开后再修改');
    return;
  }
  if (problem.status === 401) {
    ElMessage.error('管理会话已失效，请重新登录 PackBreaker');
    return;
  }
  ElMessage.error(problemText(problem));
}

async function save() {
  if (!validateDraft()) return;
  const credential = credentialPayload();
  const current = draft.id ? items.value.find((item) => item.id === draft.id) : undefined;
  if (
    current &&
    draft.type !== draft.originalType &&
    current.credential_configured &&
    !credential &&
    !draft.clearCredential
  ) {
    ElMessage.warning('切换站点类型时必须同时替换或清除已有凭证');
    return;
  }

  saving.value = true;
  try {
    if (current) {
      const patch: SitePatchInput = { clear_credential: false };
      let changed = false;
      const name = draft.name.trim();
      const baseUrl = draft.baseUrl.trim();
      if (name !== current.name) {
        patch.name = name;
        changed = true;
      }
      if (draft.type !== current.type) {
        patch.type = draft.type;
        patch.base_url = baseUrl;
        changed = true;
      } else if (baseUrl !== current.base_url) {
        patch.base_url = baseUrl;
        changed = true;
      }
      if (draft.clearCredential) {
        patch.clear_credential = true;
        changed = true;
      } else if (credential) {
        patch.credential = credential;
        changed = true;
      }
      if (!changed) {
        dialog.value = false;
        ElMessage.info('配置没有变化');
        return;
      }
      await store.update(current, patch);
      ElMessage.success('站点配置已保存；连接相关变更会自动停用并要求重新测试');
    } else {
      const payload: SiteCreateInput = {
        name: draft.name.trim(),
        type: draft.type,
        base_url: draft.baseUrl.trim(),
        ...(credential ? { credential } : {}),
      };
      await store.create(payload);
      ElMessage.success('站点配置已创建，默认保持停用');
    }
    dialog.value = false;
  } catch (caught) {
    await handleWriteProblem(caught);
  } finally {
    saving.value = false;
  }
}

async function testConnection(item: Site) {
  try {
    await store.testConnection(item);
    ElMessage.success(`${item.name} 只读连接测试通过`);
  } catch (caught) {
    await handleWriteProblem(caught);
  }
}

async function toggleEnabled(item: Site, enabled: boolean) {
  if (item.enabled === enabled) return;
  try {
    await store.setEnabled(item, enabled);
    ElMessage.success(`${item.name} 已${enabled ? '启用' : '停用'}`);
  } catch (caught) {
    await handleWriteProblem(caught);
  }
}

async function resetCircuit(item: Site) {
  try {
    await ElMessageBox.confirm(
      `重置「${item.name}」当前配置版本的熔断状态？此动作只清除进程内熔断失败计数，不会测试连接，也不会把站点标记为恢复。`,
      '确认重置站点熔断器',
      { confirmButtonText: '仅重置熔断器', cancelButtonText: '取消', type: 'warning' },
    );
  } catch {
    return;
  }
  try {
    await store.resetCircuit(item);
    ElMessage.success('熔断器已重置；站点是否恢复以之后的请求或连接测试为准');
  } catch (caught) {
    await handleWriteProblem(caught);
  }
}

async function remove(item: Site) {
  try {
    await ElMessageBox.confirm(
      `删除站点配置「${item.name}」？只删除 PackBreaker 配置与加密凭证，不会访问或修改远端站点。`,
      '确认删除站点配置',
      { confirmButtonText: '删除配置', cancelButtonText: '取消', type: 'warning' },
    );
  } catch {
    return;
  }
  try {
    await store.remove(item);
    ElMessage.success('站点配置已删除');
  } catch (caught) {
    await handleWriteProblem(caught);
  }
}
</script>

<template>
  <div>
    <div class="section-heading">
      <h2>站点配置</h2>
      <div class="downloader-heading-actions">
        <el-button :loading="loading" @click="refresh()"><RefreshCw :size="15" />刷新</el-button>
        <el-button type="primary" @click="openCreate"><Plus :size="15" />添加站点</el-button>
      </div>
    </div>

    <el-alert
      v-if="error"
      :title="error.status === 401 ? '需要管理员登录' : '站点配置暂不可用'"
      :description="problemText(error)"
      type="warning"
      :closable="false"
      show-icon
      class="section-space"
    />

    <div v-loading="loading" class="connection-cards">
      <article v-for="item in items" :key="item.id" class="panel connection-card">
        <div class="card-title">
          <span class="connection-icon"><Globe :size="25" /></span>
          <div>
            <h3>{{ item.name }}</h3>
            <small>{{ kindLabel(item.type) }} · 配置 v{{ item.version }}</small>
          </div>
          <el-switch
            :model-value="item.enabled"
            :loading="isBusy(item, 'enable')"
            :aria-label="'启用' + item.name"
            @change="toggleEnabled(item, $event === true)"
          />
        </div>
        <p class="endpoint">{{ item.base_url }}</p>
        <div class="downloader-probe-tags">
          <el-tag :type="statusTag(item.connection_status)">
            连接 {{ connectionLabel(item.connection_status) }}
          </el-tag>
          <el-tag :type="item.credential_configured ? 'success' : 'info'">
            {{ item.credential_configured ? `${credentialLabel(item.type)} 已配置` : '未配置凭证' }}
          </el-tag>
          <el-tag :type="circuitTag(health[item.id]?.circuit_state)">
            熔断 {{ circuitLabel(health[item.id]?.circuit_state) }}
          </el-tag>
        </div>
        <dl class="config-summary">
          <dt>IMDb ID 搜索</dt>
          <dd>{{ capabilityBoolean(item, 'supports_imdb_id') }}</dd>
          <dt>最小请求间隔</dt>
          <dd>
            {{
              typeof item.capabilities.min_request_interval_seconds === 'number'
                ? `${item.capabilities.min_request_interval_seconds} 秒`
                : '尚未探测'
            }}
          </dd>
          <dt>最近测试</dt>
          <dd>
            {{ item.last_test_at ? new Date(item.last_test_at).toLocaleString() : '尚未测试' }}
          </dd>
          <dt>可靠性错误</dt>
          <dd>{{ health[item.id]?.last_error_code ?? '—' }}</dd>
        </dl>
        <div class="card-actions">
          <el-button size="small" :loading="isBusy(item, 'test')" @click="testConnection(item)">
            <Activity :size="14" />测试连接
          </el-button>
          <el-button size="small" @click="openEdit(item)"> <Settings2 :size="14" />配置 </el-button>
          <el-button link type="danger" :loading="isBusy(item, 'delete')" @click="remove(item)">
            删除
          </el-button>
        </div>
      </article>
    </div>

    <el-empty v-if="!loading && !items.length && !error" description="暂无站点配置">
      <el-button type="primary" @click="openCreate">添加第一个站点</el-button>
    </el-empty>

    <section class="panel section-space">
      <h3>站点健康与可靠性</h3>
      <el-table :data="items" empty-text="暂无站点">
        <el-table-column prop="name" label="站点" min-width="150" />
        <el-table-column label="熔断状态" min-width="120">
          <template #default="{ row }">
            <el-tag :type="circuitTag(health[row.id]?.circuit_state)">
              {{ circuitLabel(health[row.id]?.circuit_state) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="失败计数" min-width="95">
          <template #default="{ row }">{{ health[row.id]?.failure_count ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="缓存命中" min-width="95">
          <template #default="{ row }">{{ cacheHitRate(health[row.id]) }}</template>
        </el-table-column>
        <el-table-column label="请求 / 重试" min-width="120">
          <template #default="{ row }">
            {{ health[row.id]?.requests_started ?? '—' }} /
            {{ health[row.id]?.retries_scheduled ?? '—' }}
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="140">
          <template #default="{ row }">
            <el-button
              link
              type="warning"
              :loading="isBusy(row, 'reset')"
              :disabled="!health[row.id]"
              @click="resetCircuit(row)"
            >
              重置熔断器
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <el-dialog
      v-model="dialog"
      :title="editing ? '编辑站点' : '添加站点'"
      width="min(680px, 94vw)"
      @closed="resetDraft"
    >
      <el-form label-position="top">
        <div class="form-grid">
          <el-form-item label="名称" required>
            <el-input v-model="draft.name" maxlength="80" />
          </el-form-item>
          <el-form-item label="站点类型" required>
            <el-select v-model="draft.type">
              <el-option label="M-Team" value="MTEAM" />
              <el-option label="HDTime" value="HDTIME" />
              <el-option label="HHClub" value="HHCLUB" />
            </el-select>
          </el-form-item>
        </div>

        <el-form-item label="HTTPS Origin" required>
          <el-input
            v-model="draft.baseUrl"
            :placeholder="
              draft.type === 'HDTIME'
                ? 'https://hdtime.org'
                : draft.type === 'HHCLUB'
                  ? 'https://hhanclub.net'
                  : 'https://kp.m-team.cc'
            "
          />
        </el-form-item>

        <el-alert
          v-if="editing && draft.credentialConfigured"
          title="凭证框留空将保留现有凭证。"
          type="info"
          :closable="false"
          class="form-alert"
        />
        <el-checkbox v-if="editing && draft.credentialConfigured" v-model="draft.clearCredential">
          清除现有凭证并保持站点停用
        </el-checkbox>

        <el-form-item v-if="!draft.clearCredential" :label="credentialLabel(draft.type)">
          <el-input
            v-model="draft.credential"
            type="password"
            show-password
            autocomplete="new-password"
            :placeholder="editing && draft.credentialConfigured ? '留空保持原凭证' : '请输入凭证'"
          />
        </el-form-item>

        <el-alert
          v-if="draft.type === 'HDTIME'"
          title="HDTime 使用 Cookie 凭证，站点地址为 https://hdtime.org。"
          type="info"
          :closable="false"
        />
        <el-alert
          v-else-if="draft.type === 'HHCLUB'"
          title="HHClub 使用 Cookie 凭证，站点地址为 https://hhanclub.net。"
          type="info"
          :closable="false"
        />
        <el-alert
          v-else
          title="M-Team 使用 API Key；站点地址示例：https://kp.m-team.cc。"
          type="info"
          :closable="false"
        />
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
      </template>
    </el-dialog>
  </div>
</template>
