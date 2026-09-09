<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue';
import { Copy, KeyRound, Plus, RefreshCw, ShieldCheck, Trash2 } from '@lucide/vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { useApiTokenStore } from '../stores/automationAccess';
import type { ApiScope, ApiTokenView } from '../api/automationAccess';

const store = useApiTokenStore();
const createVisible = ref(false);
const secretVisible = ref(false);
const createdToken = ref('');
const createdName = ref('');
const scopeOptions: ApiScope[] = ['tasks:read', 'tasks:write', 'config:read', 'config:write'];
const draft = reactive({
  name: '',
  scopes: ['config:read'] as ApiScope[],
  expiresAt: new Date(Date.now() + 30 * 24 * 60 * 60 * 1000),
});
const activeCount = computed(
  () =>
    store.items.filter(
      (item) => !item.revoked_at && new Date(item.expires_at).getTime() > Date.now(),
    ).length,
);

function formatTime(value: string | null): string {
  if (!value) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value));
}

function stateOf(item: ApiTokenView): { text: string; type: 'success' | 'info' | 'warning' } {
  if (item.revoked_at) return { text: '已撤销', type: 'info' };
  if (new Date(item.expires_at).getTime() <= Date.now()) return { text: '已过期', type: 'warning' };
  return { text: '有效', type: 'success' };
}

function openCreate(): void {
  draft.name = '';
  draft.scopes = ['config:read'];
  draft.expiresAt = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000);
  createVisible.value = true;
}

async function createToken(): Promise<void> {
  if (!draft.name.trim()) {
    ElMessage.warning('请填写 Token 名称');
    return;
  }
  if (!draft.scopes.length) {
    ElMessage.warning('至少选择一个权限范围');
    return;
  }
  if (draft.expiresAt.getTime() <= Date.now()) {
    ElMessage.warning('过期时间必须在未来');
    return;
  }
  try {
    const created = await store.create({
      name: draft.name.trim(),
      scopes: [...draft.scopes],
      expires_at: draft.expiresAt.toISOString(),
    });
    createVisible.value = false;
    createdName.value = created.name;
    createdToken.value = created.token;
    secretVisible.value = true;
  } catch {}
}

async function copyToken(): Promise<void> {
  if (!createdToken.value) return;
  try {
    await navigator.clipboard.writeText(createdToken.value);
    ElMessage.success('Token 已复制到剪贴板');
  } catch {
    ElMessage.warning('浏览器未允许自动复制，请手动选择 Token');
  }
}

function clearSecret(): void {
  createdToken.value = '';
  createdName.value = '';
}

async function revoke(item: ApiTokenView): Promise<void> {
  if (item.revoked_at) return;
  try {
    await ElMessageBox.confirm(
      `撤销 API Token「${item.name}」？使用该 Token 的自动化调用会立即失效。`,
      '确认撤销',
      { confirmButtonText: '撤销 Token', cancelButtonText: '取消', type: 'warning' },
    );
    await store.revoke(item.id);
    ElMessage.success('API Token 已撤销');
  } catch {}
}

onMounted(() => {
  void store.refresh().catch(() => undefined);
});
onUnmounted(() => {
  clearSecret();
  store.clear();
});
</script>

<template>
  <section class="token-section">
    <div class="section-heading compact-heading">
      <div>
        <h3>API Token</h3>
        <p class="muted">供脚本与自动化调用使用；明文只在创建成功后展示一次。</p>
      </div>
      <div class="token-actions">
        <el-button :loading="store.loading" @click="store.refresh">
          <RefreshCw :size="14" />刷新
        </el-button>
        <el-button type="primary" @click="openCreate"><Plus :size="14" />创建 Token</el-button>
      </div>
    </div>
    <el-alert
      v-if="store.error"
      :title="store.error.message"
      type="error"
      :closable="false"
      show-icon
      class="section-space"
    />
    <div class="token-summary">
      <KeyRound :size="18" />当前有效 Token <b>{{ activeCount }}</b>
      <span>共 {{ store.items.length }} 条记录</span>
    </div>
    <el-table v-loading="store.loading" :data="store.items" empty-text="尚未创建 API Token">
      <el-table-column prop="name" label="名称" min-width="150" />
      <el-table-column label="权限范围" min-width="230">
        <template #default="{ row }">
          <el-tag v-for="scope in row.scopes" :key="scope" class="scope-tag">{{ scope }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="stateOf(row).type">{{ stateOf(row).text }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="过期时间" min-width="160">
        <template #default="{ row }">{{ formatTime(row.expires_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90">
        <template #default="{ row }">
          <el-button
            link
            type="danger"
            :disabled="Boolean(row.revoked_at)"
            :loading="store.busy"
            @click="revoke(row)"
          >
            <Trash2 :size="13" />撤销
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="createVisible" title="创建 API Token" width="min(560px, 94vw)">
      <el-form label-position="top">
        <el-form-item label="名称" required>
          <el-input
            v-model="draft.name"
            maxlength="80"
            placeholder="例如：home-assistant-readonly"
          />
        </el-form-item>
        <el-form-item label="权限范围" required>
          <el-checkbox-group v-model="draft.scopes">
            <el-checkbox v-for="scope in scopeOptions" :key="scope" :value="scope">{{
              scope
            }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="过期时间" required>
          <el-date-picker v-model="draft.expiresAt" type="datetime" style="width: 100%" />
        </el-form-item>
        <el-alert
          title="建议只授予必要 scope，并使用尽可能短的有效期。Token 明文不会保存到浏览器存储。"
          type="info"
          :closable="false"
          show-icon
        />
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="store.busy" @click="createToken">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="secretVisible"
      title="保存新的 API Token"
      width="min(620px, 94vw)"
      :close-on-click-modal="false"
      @closed="clearSecret"
    >
      <el-alert
        title="这是唯一一次显示 Token 明文。关闭后 PackBreaker 无法再次读取，请立即保存到安全位置。"
        type="warning"
        :closable="false"
        show-icon
      />
      <div class="token-secret">
        <div><ShieldCheck :size="17" />{{ createdName }}</div>
        <code>{{ createdToken }}</code>
      </div>
      <template #footer>
        <el-button @click="copyToken"><Copy :size="14" />复制 Token</el-button>
        <el-button type="primary" @click="secretVisible = false">我已保存</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.token-section {
  margin-top: 22px;
}
.compact-heading {
  align-items: flex-end;
}
.compact-heading h3,
.compact-heading p {
  margin: 0;
}
.compact-heading p {
  margin-top: 6px;
}
.token-actions {
  display: flex;
  gap: 8px;
}
.token-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 13px 15px;
  margin: 16px 0;
  background: var(--canvas);
  border: 1px solid var(--line);
  border-radius: 6px;
  font-size: 12px;
}
.token-summary svg {
  color: var(--blue);
}
.token-summary span {
  margin-left: auto;
  color: var(--muted);
}
.scope-tag {
  margin: 2px 5px 2px 0;
}
.token-secret {
  margin-top: 18px;
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--canvas);
}
.token-secret > div {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  color: var(--muted);
}
.token-secret code {
  display: block;
  word-break: break-all;
  user-select: all;
  font-size: 12px;
  line-height: 1.7;
}
@media (max-width: 640px) {
  .compact-heading {
    align-items: stretch;
  }
  .token-actions {
    width: 100%;
  }
  .token-actions .el-button {
    flex: 1;
  }
}
</style>
