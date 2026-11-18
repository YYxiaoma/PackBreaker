<script setup lang="ts">
import { Box, ExternalLink, GitBranch, RefreshCw, ShieldCheck } from '@lucide/vue';
import { computed, onMounted, ref } from 'vue';

import { getReleaseNotes, getSystemHealth, type ReleaseNoteEntry } from '../api/system';

const emit = defineEmits<{
  'open-version': [];
}>();

const version = ref('—');

// 升级日志：默认只展开最近几个版本，其余折叠，避免关于页被历史条目淹没。
const VISIBLE_RELEASE_NOTES = 3;
const releaseNotes = ref<ReleaseNoteEntry[]>([]);
const releaseNotesLoading = ref(false);
const releaseNotesFailed = ref(false);
const showAllReleaseNotes = ref(false);
const visibleReleaseNotes = computed(() =>
  showAllReleaseNotes.value
    ? releaseNotes.value
    : releaseNotes.value.slice(0, VISIBLE_RELEASE_NOTES),
);
const hiddenReleaseNoteCount = computed(() =>
  Math.max(0, releaseNotes.value.length - VISIBLE_RELEASE_NOTES),
);

async function loadReleaseNotes(): Promise<void> {
  releaseNotesLoading.value = true;
  releaseNotesFailed.value = false;
  try {
    releaseNotes.value = (await getReleaseNotes()).entries;
  } catch {
    releaseNotesFailed.value = true;
  } finally {
    releaseNotesLoading.value = false;
  }
}

onMounted(async () => {
  void loadReleaseNotes();
  try {
    version.value = (await getSystemHealth()).version;
  } catch {
    // 关于页不应因为健康接口短暂失败而阻断其他静态项。
  }
});
</script>

<template>
  <div class="about-page">
    <section class="panel about-hero">
      <span class="about-logo"><Box :size="34" /></span>
      <div class="about-copy">
        <p class="about-kicker">PackBreaker</p>
        <h2>面向 PT 场景的自动拆包辅种系统</h2>
        <p class="muted">
          在保留源数据、人工确认、幂等与 operation journal 安全链的前提下，
          集中管理拆包任务、站点、下载器、通知与只读 AI 助手。
        </p>
        <div class="about-actions">
          <el-button type="primary" @click="emit('open-version')">
            <RefreshCw :size="15" />检查更新
          </el-button>
          <a
            class="about-link"
            href="https://github.com/YYxiaoma/PackBreaker"
            target="_blank"
            rel="noopener noreferrer"
          >
            <GitBranch :size="16" />GitHub 项目<ExternalLink :size="13" />
          </a>
        </div>
      </div>
      <div class="about-version">
        <small>当前版本</small>
        <strong>v{{ version }}</strong>
      </div>
    </section>

    <section class="about-grid section-space">
      <article class="panel about-card">
        <span class="file-icon"><ShieldCheck :size="20" /></span>
        <div>
          <h3>安全原则</h3>
          <p>
            高风险写操作继续经过既有预演、人工确认、幂等和 journal-backed 执行链；v0.1.6 AI
            助手保持只读，不提供绕过这些安全门的入口。
          </p>
        </div>
      </article>
      <article class="panel about-card">
        <span class="file-icon"><Box :size="20" /></span>
        <div>
          <h3>构建与许可</h3>
          <p>运行版本：v{{ version }}</p>
          <p>开源许可证：MIT License</p>
          <p class="muted">Copyright © 2026 YYxiaoma</p>
        </div>
      </article>
    </section>

    <section class="panel about-changelog section-space">
      <header class="about-changelog-head">
        <div>
          <h3>升级日志</h3>
          <p class="muted">随版本发布的变更说明，按版本倒序排列。</p>
        </div>
        <small v-if="releaseNotes.length">共 {{ releaseNotes.length }} 个版本</small>
      </header>

      <p v-if="releaseNotesLoading && !releaseNotes.length" class="about-changelog-empty">
        正在加载升级日志…
      </p>
      <p v-else-if="releaseNotesFailed && !releaseNotes.length" class="about-changelog-empty">
        升级日志暂时不可用，不影响其他功能。
        <el-button link type="primary" @click="loadReleaseNotes">重试</el-button>
      </p>
      <p v-else-if="!releaseNotes.length" class="about-changelog-empty">当前镜像未附带升级日志。</p>

      <template v-else>
        <article
          v-for="entry in visibleReleaseNotes"
          :key="entry.version"
          class="changelog-entry"
          :class="{ 'is-current': entry.is_current }"
        >
          <div class="changelog-entry-head">
            <strong>v{{ entry.version }}</strong>
            <span v-if="entry.is_current" class="changelog-badge">当前版本</span>
            <small v-if="entry.released_on">{{ entry.released_on }}</small>
          </div>
          <p v-if="entry.summary" class="changelog-summary">{{ entry.summary }}</p>
          <div v-for="section in entry.sections" :key="section.title" class="changelog-section">
            <b>{{ section.title }}</b>
            <ul>
              <li v-for="(item, index) in section.items" :key="index">{{ item }}</li>
            </ul>
          </div>
        </article>
        <el-button
          v-if="hiddenReleaseNoteCount && !showAllReleaseNotes"
          class="changelog-more"
          link
          type="primary"
          @click="showAllReleaseNotes = true"
        >
          展开其余 {{ hiddenReleaseNoteCount }} 个版本
        </el-button>
      </template>
    </section>
  </div>
</template>

<style scoped>
.about-page {
  display: grid;
}
.about-hero {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 20px;
}
.about-logo {
  display: grid;
  width: 62px;
  height: 62px;
  place-items: center;
  border-radius: 18px;
  background: var(--accent-soft);
  color: var(--accent);
}
.about-copy h2,
.about-copy p,
.about-card h3,
.about-card p {
  margin: 0;
}
.about-kicker {
  margin-bottom: 6px !important;
  color: var(--accent);
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.about-copy h2 {
  margin-bottom: 8px;
  font-size: 24px;
}
.about-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 18px;
}
.about-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text);
  font-weight: 700;
  text-decoration: none;
}
.about-link:hover {
  color: var(--accent);
}
.about-version {
  display: grid;
  min-width: 120px;
  gap: 3px;
  text-align: right;
}
.about-version small {
  color: var(--muted);
}
.about-version strong {
  font-size: 22px;
}
.about-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.about-changelog {
  display: grid;
  gap: 4px;
}
.about-changelog-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 4px;
}
.about-changelog-head h3 {
  margin: 0 0 4px;
}
.about-changelog-head p {
  margin: 0;
  line-height: 1.6;
}
.about-changelog-head small {
  flex: none;
  color: var(--muted);
  font-size: 12px;
}
.about-changelog-empty {
  margin: 8px 0 0;
  color: var(--muted);
  font-size: 13px;
}
.changelog-entry {
  display: grid;
  gap: 8px;
  padding: 14px 0;
  border-top: 1px solid var(--line);
}
.changelog-entry:first-of-type {
  padding-top: 10px;
  border-top: 0;
}
.changelog-entry-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.changelog-entry-head strong {
  font-size: 16px;
}
.changelog-entry-head small {
  color: var(--muted);
  font-size: 12px;
}
.changelog-badge {
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11px;
  font-weight: 700;
}
.changelog-summary {
  margin: 0;
  line-height: 1.7;
}
.changelog-section b {
  display: block;
  margin-bottom: 4px;
  color: var(--accent);
  font-size: 13px;
}
.changelog-section ul {
  display: grid;
  gap: 4px;
  margin: 0;
  padding-left: 18px;
}
.changelog-section li {
  color: var(--muted);
  font-size: 13px;
  line-height: 1.7;
}
.changelog-more {
  justify-self: start;
  margin-top: 4px;
}
.about-card {
  display: flex;
  align-items: flex-start;
  gap: 14px;
}
.about-card h3 {
  margin-bottom: 8px;
}
.about-card p {
  line-height: 1.7;
}
@media (max-width: 720px) {
  .about-hero {
    grid-template-columns: 1fr;
  }
  .about-version {
    text-align: left;
  }
  .about-grid {
    grid-template-columns: 1fr;
  }
  .about-actions {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
