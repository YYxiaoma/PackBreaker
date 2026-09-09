<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { storeToRefs } from 'pinia';
import { ElMessage, ElMessageBox } from 'element-plus';
import {
  Activity,
  AlertTriangle,
  Check,
  HardDrive,
  Plus,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Trash2,
} from '@lucide/vue';
import { ApiProblem, toApiProblem } from '../api/client';
import type {
  Downloader,
  DownloaderCredential,
  DownloaderKind,
  DownloaderPatchInput,
  PathDiagnosticProbeInput,
  PathDiagnosticReport,
  PathMapping,
} from '../api/downloaders';
import { useDownloaderStore } from '../stores/downloaders';

type CredentialMode = 'password' | 'api_key';

interface DownloaderDraft {
  id: string | null;
  originalType: DownloaderKind | null;
  name: string;
  type: DownloaderKind;
  baseUrl: string;
  category: string;
  mappings: PathMapping[];
  credentialMode: CredentialMode;
  username: string;
  password: string;
  apiKey: string;
  clearCredential: boolean;
  credentialConfigured: boolean;
}

interface DiagnosticDraft {
  mapping: PathMapping;
  remote_path: string;
  target_directory: string;
}

const store = useDownloaderStore();
const { items, loading, error, busy, diagnostics } = storeToRefs(store);
const dialog = ref(false);
const saving = ref(false);
const diagnosticDialog = ref(false);
const diagnosticTargetId = ref<string | null>(null);
const diagnosticDrafts = ref<DiagnosticDraft[]>([]);

const draft = reactive<DownloaderDraft>({
  id: null,
  originalType: null,
  name: '',
  type: 'QBITTORRENT',
  baseUrl: '',
  category: '',
  mappings: [],
  credentialMode: 'password',
  username: '',
  password: '',
  apiKey: '',
  clearCredential: false,
  credentialConfigured: false,
});

const editing = computed(() => draft.id !== null);
const diagnosticTarget = computed(() =>
  items.value.find((item) => item.id === diagnosticTargetId.value),
);
const diagnosticReport = computed<PathDiagnosticReport | null>(() => {
  if (!diagnosticTargetId.value) return null;
  return diagnostics.value[diagnosticTargetId.value] ?? null;
});

onMounted(() => {
  void refresh(false);
});

function kindLabel(kind: DownloaderKind): string {
  return kind === 'QBITTORRENT' ? 'qBittorrent' : 'Transmission';
}

function statusLabel(status: Downloader['connection_status']): string {
  if (status === 'OK') return '已验证';
  if (status === 'FAILED') return '失败';
  return '未测试';
}

function statusTag(status: Downloader['connection_status']): 'success' | 'danger' | 'info' {
  if (status === 'OK') return 'success';
  if (status === 'FAILED') return 'danger';
  return 'info';
}

function capability(item: Downloader, key: string): string | null {
  const value = item.capabilities[key];
  return typeof value === 'string' && value ? value : null;
}

function category(item: Downloader): string {
  const value = item.monitor_rules.category;
  return typeof value === 'string' && value ? value : '未设置';
}

function isBusy(item: Downloader, operation: string): boolean {
  return busy.value[`${operation}:${item.id}`] === true;
}

function problemText(problem: ApiProblem): string {
  return problem.traceId ? `${problem.message} · trace_id ${problem.traceId}` : problem.message;
}

async function refresh(notify = true) {
  try {
    await store.refresh();
    if (notify) ElMessage.success('下载器配置已刷新');
  } catch (caught) {
    if (notify) ElMessage.error(problemText(toApiProblem(caught)));
  }
}

function resetDraft() {
  Object.assign(draft, {
    id: null,
    originalType: null,
    name: '',
    type: 'QBITTORRENT' as DownloaderKind,
    baseUrl: '',
    category: '',
    mappings: [] as PathMapping[],
    credentialMode: 'password' as CredentialMode,
    username: '',
    password: '',
    apiKey: '',
    clearCredential: false,
    credentialConfigured: false,
  });
}

function openCreate() {
  resetDraft();
  draft.mappings.push({ remote_prefix: '/downloads', container_prefix: '/data' });
  dialog.value = true;
}

function openEdit(item: Downloader) {
  resetDraft();
  Object.assign(draft, {
    id: item.id,
    originalType: item.type,
    name: item.name,
    type: item.type,
    baseUrl: item.base_url,
    category: category(item) === '未设置' ? '' : category(item),
    mappings: item.path_mappings.map((mapping) => ({ ...mapping })),
    credentialConfigured: item.credential_configured,
  });
  dialog.value = true;
}

function addMapping() {
  draft.mappings.push({ remote_prefix: '', container_prefix: '/data' });
}

function removeMapping(index: number) {
  draft.mappings.splice(index, 1);
}

function credentialPayload(): DownloaderCredential | undefined | null {
  if (draft.clearCredential) return undefined;
  if (draft.type === 'QBITTORRENT' && draft.credentialMode === 'api_key') {
    if (!draft.apiKey.trim()) return undefined;
    return { api_key: draft.apiKey };
  }
  const hasUsername = Boolean(draft.username.trim());
  const hasPassword = Boolean(draft.password);
  if (!hasUsername && !hasPassword) return undefined;
  if (!hasUsername || !hasPassword) {
    ElMessage.warning('用户名和密码必须同时填写');
    return null;
  }
  return { username: draft.username.trim(), password: draft.password };
}

function validateDraft(): boolean {
  if (!draft.name.trim() || !/^https?:\/\//i.test(draft.baseUrl.trim())) {
    ElMessage.warning('请输入名称和合法的 HTTP / HTTPS 管理地址');
    return false;
  }
  if (
    draft.mappings.some(
      (mapping) => !mapping.remote_prefix.trim() || !mapping.container_prefix.trim(),
    )
  ) {
    ElMessage.warning('路径映射的两侧都必须填写');
    return false;
  }
  return true;
}

function normalizedMappings(): PathMapping[] {
  return draft.mappings.map((mapping) => ({
    remote_prefix: mapping.remote_prefix.trim(),
    container_prefix: mapping.container_prefix.trim(),
  }));
}

function mappingsEqual(left: PathMapping[], right: PathMapping[]): boolean {
  return (
    left.length === right.length &&
    left.every(
      (mapping, index) =>
        mapping.remote_prefix === right[index]?.remote_prefix &&
        mapping.container_prefix === right[index]?.container_prefix,
    )
  );
}

async function handleWriteProblem(caught: unknown) {
  const problem = toApiProblem(caught);
  if (problem.status === 412) {
    await store.refresh().catch(() => undefined);
    ElMessage.warning('配置已被其他请求更新，已刷新列表，请重新打开后再修改');
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
  if (credential === null) return;
  const current = draft.id ? items.value.find((item) => item.id === draft.id) : undefined;
  if (
    current &&
    draft.type !== draft.originalType &&
    current.credential_configured &&
    credential === undefined &&
    !draft.clearCredential
  ) {
    ElMessage.warning('切换下载器类型时必须同时替换或清除已有凭证');
    return;
  }

  saving.value = true;
  try {
    const nextMappings = normalizedMappings();
    const nextRules = draft.category.trim() ? { category: draft.category.trim() } : {};
    const common = {
      name: draft.name.trim(),
      type: draft.type,
      base_url: draft.baseUrl.trim(),
      monitor_rules: nextRules,
      path_mappings: nextMappings,
    };
    if (current) {
      const patch: DownloaderPatchInput = {};
      if (common.name !== current.name) patch.name = common.name;
      if (common.type !== current.type) patch.type = common.type;
      if (common.base_url !== current.base_url) patch.base_url = common.base_url;
      if (JSON.stringify(common.monitor_rules) !== JSON.stringify(current.monitor_rules)) {
        patch.monitor_rules = common.monitor_rules;
      }
      if (!mappingsEqual(common.path_mappings, current.path_mappings)) {
        patch.path_mappings = common.path_mappings;
      }
      if (draft.clearCredential) patch.clear_credential = true;
      else if (credential) patch.credential = credential;
      if (!Object.keys(patch).length) {
        dialog.value = false;
        ElMessage.info('配置没有变化');
        return;
      }
      await store.update(current, patch);
      ElMessage.success('下载器配置已保存；安全相关变更会自动重新要求探测');
    } else {
      await store.create({ ...common, ...(credential ? { credential } : {}) });
      ElMessage.success('下载器配置已创建，默认保持停用');
    }
    dialog.value = false;
  } catch (caught) {
    await handleWriteProblem(caught);
  } finally {
    saving.value = false;
  }
}

async function testConnection(item: Downloader) {
  try {
    const result = await store.testConnection(item);
    const version = result.capabilities.version;
    ElMessage.success(
      `${item.name} 只读连接测试通过${typeof version === 'string' ? ` · ${version}` : ''}`,
    );
  } catch (caught) {
    await handleWriteProblem(caught);
  }
}

function openDiagnostics(item: Downloader) {
  if (!item.path_mappings.length) {
    ElMessage.warning('请先配置至少一条路径映射');
    return;
  }
  diagnosticTargetId.value = item.id;
  diagnosticDrafts.value = item.path_mappings.map((mapping) => ({
    mapping: { ...mapping },
    remote_path: '',
    target_directory: '',
  }));
  diagnosticDialog.value = true;
}

async function runDiagnostics() {
  const item = diagnosticTarget.value;
  if (!item) return;
  if (
    diagnosticDrafts.value.some(
      (probe) => !probe.remote_path.trim() || !probe.target_directory.trim(),
    )
  ) {
    ElMessage.warning('每条路径映射都需要提供一个真实存在的测试文件和目标目录');
    return;
  }
  const probes: PathDiagnosticProbeInput[] = diagnosticDrafts.value.map((probe) => ({
    remote_path: probe.remote_path.trim(),
    target_directory: probe.target_directory.trim(),
  }));
  try {
    const report = await store.diagnose(item, probes);
    if (report.status === 'ok') ElMessage.success('全部路径映射已通过真实文件系统诊断');
    else ElMessage.warning(`路径诊断被阻断：${report.error_code ?? '未知原因'}`);
  } catch (caught) {
    await handleWriteProblem(caught);
  }
}

async function toggleEnabled(item: Downloader, enabled: boolean) {
  if (item.enabled === enabled) return;
  try {
    await store.setEnabled(item, enabled);
    ElMessage.success(`${item.name} 已${enabled ? '启用' : '停用'}`);
  } catch (caught) {
    await handleWriteProblem(caught);
  }
}

async function remove(item: Downloader) {
  try {
    await ElMessageBox.confirm(
      `删除下载器配置「${item.name}」？只删除 PackBreaker 配置和加密凭证，不会删除下载器中的任务或数据。`,
      '确认删除下载器配置',
      { confirmButtonText: '删除配置', cancelButtonText: '取消', type: 'warning' },
    );
  } catch {
    return;
  }
  try {
    await store.remove(item);
    ElMessage.success('下载器配置已删除');
  } catch (caught) {
    await handleWriteProblem(caught);
  }
}
</script>

<template>
  <div>
    <div class="section-heading">
      <h2>下载器实例 <small>真实配置 API · M1 仅执行只读连接探测与路径诊断</small></h2>
      <div class="downloader-heading-actions">
        <el-button :loading="loading" @click="refresh()"><RefreshCw :size="15" />刷新</el-button>
        <el-button type="primary" @click="openCreate"><Plus :size="15" />添加下载器</el-button>
      </div>
    </div>

    <el-alert
      v-if="error"
      :title="error.status === 401 ? '需要管理员登录' : '下载器 API 暂不可用'"
      :description="problemText(error)"
      type="warning"
      :closable="false"
      show-icon
      class="section-space"
    />

    <div v-loading="loading" class="connection-cards real-downloader-cards">
      <article v-for="item in items" :key="item.id" class="panel connection-card">
        <div class="card-title">
          <span class="connection-icon"><HardDrive :size="25" /></span>
          <div>
            <h3>{{ item.name }}</h3>
            <small>{{ kindLabel(item.type) }} · v{{ item.version }}</small>
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
            连接 {{ statusLabel(item.connection_status) }}
          </el-tag>
          <el-tag :type="statusTag(item.path_mapping_status)">
            路径 {{ statusLabel(item.path_mapping_status) }}
          </el-tag>
          <el-tag :type="item.credential_configured ? 'success' : 'info'">
            {{ item.credential_configured ? '凭证已加密配置' : '未配置凭证' }}
          </el-tag>
        </div>
        <dl class="config-summary">
          <dt>客户端版本</dt>
          <dd>{{ capability(item, 'version') ?? '尚未探测' }}</dd>
          <dt>API / RPC 版本</dt>
          <dd>{{ capability(item, 'api_version') ?? '—' }}</dd>
          <dt>监控分类 / 标签</dt>
          <dd>{{ category(item) }}</dd>
          <dt>路径映射</dt>
          <dd>{{ item.path_mappings.length }} 条</dd>
        </dl>
        <div v-if="item.path_mappings.length" class="mapping-list">
          <code v-for="(mapping, index) in item.path_mappings" :key="index">
            {{ mapping.remote_prefix }} → {{ mapping.container_prefix }}
          </code>
        </div>
        <div class="card-actions">
          <el-button size="small" :loading="isBusy(item, 'test')" @click="testConnection(item)">
            <Activity :size="14" />测试连接
          </el-button>
          <el-button size="small" :loading="isBusy(item, 'diagnose')" @click="openDiagnostics(item)"
            >路径诊断</el-button
          >
          <el-button size="small" @click="openEdit(item)"> <Settings2 :size="14" />配置 </el-button>
          <el-button link type="danger" :loading="isBusy(item, 'delete')" @click="remove(item)"
            >删除</el-button
          >
        </div>
      </article>
    </div>

    <el-empty
      v-if="!loading && !items.length && !error"
      description="还没有下载器配置。创建后需分别完成连接测试和全部路径映射诊断才能启用。"
    >
      <el-button type="primary" @click="openCreate">添加第一个下载器</el-button>
    </el-empty>

    <section class="panel section-space downloader-boundary">
      <ShieldCheck :size="21" />
      <div>
        <b>M1 安全边界</b>
        <p>
          当前界面只管理配置、执行版本/认证探测和临时 hardlink
          路径诊断。不会添加、删除、暂停、恢复或校验下载器任务，也不会删除下载数据。
        </p>
      </div>
    </section>

    <el-dialog
      v-model="dialog"
      :title="editing ? '编辑下载器' : '添加下载器'"
      width="min(720px, 94vw)"
      @closed="resetDraft"
    >
      <el-form label-position="top">
        <div class="form-grid">
          <el-form-item label="名称" required>
            <el-input v-model="draft.name" maxlength="80" />
          </el-form-item>
          <el-form-item label="下载器类型" required>
            <el-select v-model="draft.type">
              <el-option label="qBittorrent" value="QBITTORRENT" />
              <el-option label="Transmission" value="TRANSMISSION" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item label="管理地址" required>
          <el-input v-model="draft.baseUrl" placeholder="http://127.0.0.1:8080" />
        </el-form-item>

        <el-alert
          v-if="editing && draft.credentialConfigured"
          title="现有凭证不会从服务端回显。下方留空表示保持原凭证。"
          type="info"
          :closable="false"
          class="form-alert"
        />
        <el-checkbox v-if="editing && draft.credentialConfigured" v-model="draft.clearCredential"
          >清除现有凭证并保持下载器停用</el-checkbox
        >

        <template v-if="!draft.clearCredential">
          <el-form-item v-if="draft.type === 'QBITTORRENT'" label="凭证方式">
            <el-radio-group v-model="draft.credentialMode">
              <el-radio value="password">用户名 / 密码</el-radio>
              <el-radio value="api_key">API Key</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item
            v-if="draft.type === 'QBITTORRENT' && draft.credentialMode === 'api_key'"
            label="API Key"
          >
            <el-input
              v-model="draft.apiKey"
              type="password"
              show-password
              autocomplete="new-password"
              placeholder="仅写入加密 secret store"
            />
          </el-form-item>
          <div v-else class="form-grid">
            <el-form-item label="用户名">
              <el-input v-model="draft.username" autocomplete="username" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input
                v-model="draft.password"
                type="password"
                show-password
                autocomplete="new-password"
              />
            </el-form-item>
          </div>
        </template>

        <el-form-item label="监控分类 / 标签">
          <el-input v-model="draft.category" placeholder="例如：大包" />
        </el-form-item>

        <div class="mapping-editor-heading">
          <b>路径映射</b>
          <el-button size="small" @click="addMapping"><Plus :size="14" />添加规则</el-button>
        </div>
        <div v-for="(mapping, index) in draft.mappings" :key="index" class="mapping-editor-row">
          <el-input v-model="mapping.remote_prefix" placeholder="下载器路径，例如 /downloads" />
          <span>→</span>
          <el-input
            v-model="mapping.container_prefix"
            placeholder="容器路径，例如 /data/downloads"
          />
          <el-button link type="danger" aria-label="删除路径映射" @click="removeMapping(index)">
            <Trash2 :size="16" />
          </el-button>
        </div>
        <p class="muted">
          配置可以先保存为停用状态；启用前必须对每条映射提交真实存在的测试文件并通过诊断。
        </p>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="diagnosticDialog" title="路径映射诊断" width="min(760px, 94vw)">
      <template v-if="diagnosticTarget">
        <el-alert
          title="诊断发生在 PackBreaker 服务端文件系统。请为每条映射填写一个真实存在的下载器文件路径和一个已存在的目标目录。"
          type="warning"
          :closable="false"
        />
        <div v-for="(probe, index) in diagnosticDrafts" :key="index" class="diagnostic-probe">
          <b
            >映射 {{ index + 1 }}：{{ probe.mapping.remote_prefix }} →
            {{ probe.mapping.container_prefix }}</b
          >
          <el-form label-position="top">
            <el-form-item label="下载器视角的已存在测试文件">
              <el-input
                v-model="probe.remote_path"
                :placeholder="`${probe.mapping.remote_prefix}/实际文件.mkv`"
              />
            </el-form-item>
            <el-form-item label="容器内已存在目标目录">
              <el-input v-model="probe.target_directory" placeholder="/data/seeding/test-target" />
            </el-form-item>
          </el-form>
        </div>

        <div v-if="diagnosticReport" class="diagnostic-report">
          <el-alert
            :title="
              diagnosticReport.status === 'ok'
                ? '全部映射诊断通过，可以满足路径启用门槛'
                : `诊断被阻断：${diagnosticReport.error_code ?? '未知原因'}`
            "
            :type="diagnosticReport.status === 'ok' ? 'success' : 'error'"
            :closable="false"
          />
          <div
            v-for="(result, index) in diagnosticReport.results"
            :key="index"
            class="diagnostic-result"
          >
            <component :is="result.status === 'ok' ? Check : AlertTriangle" :size="18" />
            <span>规则 {{ result.rule_index + 1 }}</span>
            <span>设备 {{ result.source_device }} → {{ result.target_device }}</span>
            <el-tag :type="result.hardlink_feasible ? 'success' : 'danger'">
              {{ result.hardlink_feasible ? 'hardlink 可行' : (result.error_code ?? '不可行') }}
            </el-tag>
          </div>
        </div>
      </template>
      <template #footer>
        <el-button @click="diagnosticDialog = false">关闭</el-button>
        <el-button
          v-if="diagnosticTarget"
          type="primary"
          :loading="isBusy(diagnosticTarget, 'diagnose')"
          @click="runDiagnostics"
          >运行真实诊断</el-button
        >
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.downloader-heading-actions,
.downloader-probe-tags,
.mapping-editor-heading,
.diagnostic-result {
  display: flex;
  align-items: center;
  gap: 8px;
}

.downloader-probe-tags {
  flex-wrap: wrap;
  margin: 12px 0;
}

.mapping-list {
  display: grid;
  gap: 6px;
  margin: 12px 0;
}

.mapping-list code {
  overflow-wrap: anywhere;
  white-space: normal;
}

.downloader-boundary {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}

.downloader-boundary p {
  margin: 5px 0 0;
}

.form-alert {
  margin-bottom: 14px;
}

.mapping-editor-heading {
  justify-content: space-between;
  margin: 8px 0 10px;
}

.mapping-editor-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.diagnostic-probe {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.diagnostic-report {
  display: grid;
  gap: 10px;
  margin-top: 18px;
}

.diagnostic-result {
  flex-wrap: wrap;
  padding: 10px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

@media (max-width: 700px) {
  .downloader-heading-actions {
    width: 100%;
    justify-content: flex-end;
  }

  .mapping-editor-row {
    grid-template-columns: 1fr auto;
  }

  .mapping-editor-row > :nth-child(2) {
    display: none;
  }

  .mapping-editor-row > :nth-child(3) {
    grid-column: 1;
  }
}
</style>
