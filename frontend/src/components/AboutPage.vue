<script setup lang="ts">
import { Box, ChevronDown, ExternalLink, GitBranch, RefreshCw, ShieldCheck } from '@lucide/vue';
import { onMounted, ref } from 'vue';

import { getReleaseNotes, getSystemHealth, type ReleaseNoteEntry } from '../api/system';

const emit = defineEmits<{
  'open-version': [];
}>();

const version = ref('—');

// 升级日志：一个版本一张卡片，默认只展开当前版本，其余版本折叠为一行摘要。
const releaseNotes = ref<ReleaseNoteEntry[]>([]);
const releaseNotesLoading = ref(false);
const releaseNotesFailed = ref(false);
const expandedVersions = ref<string[]>([]);

function isExpanded(entryVersion: string): boolean {
  return expandedVersions.value.includes(entryVersion);
}

function toggleVersion(entryVersion: string): void {
  expandedVersions.value = isExpanded(entryVersion)
    ? expandedVersions.value.filter((item) => item !== entryVersion)
    : [...expandedVersions.value, entryVersion];
}

function changeCount(entry: ReleaseNoteEntry): number {
  return entry.sections.reduce((total, section) => total + section.items.length, 0);
}

function bodyId(entryVersion: string): string {
  return `changelog-body-${entryVersion.replace(/[^\w-]/g, '-')}`;
}

// 默认展开的卡片：优先当前运行版本，其次最新一条（列表已按版本倒序）。
function defaultExpandedVersion(entries: ReleaseNoteEntry[]): string | null {
  return entries.find((entry) => entry.is_current)?.version ?? entries[0]?.version ?? null;
}

async function loadReleaseNotes(): Promise<void> {
  releaseNotesLoading.value = true;
  releaseNotesFailed.value = false;
  try {
    const entries = (await getReleaseNotes()).entries;
    releaseNotes.value = entries;
    const initial = defaultExpandedVersion(entries);
    expandedVersions.value = initial ? [initial] : [];
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

    <section class="about-changelog section-space">
      <header class="about-changelog-head">
        <div>
          <h3>升级日志</h3>
          <p class="muted">一个版本一张卡片，点击版本展开或收起该版本的变更明细。</p>
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

      <div v-else class="changelog-list">
        <article
          v-for="entry in releaseNotes"
          :key="entry.version"
          class="changelog-card"
          :class="{ 'is-current': entry.is_current, 'is-open': isExpanded(entry.version) }"
        >
          <button
            type="button"
            class="changelog-card-head"
            :aria-expanded="isExpanded(entry.version) ? 'true' : 'false'"
            :aria-controls="bodyId(entry.version)"
            @click="toggleVersion(entry.version)"
          >
            <span class="changelog-version">v{{ entry.version }}</span>
            <span v-if="entry.is_current" class="changelog-badge">当前版本</span>
            <small v-if="entry.released_on" class="changelog-date">{{ entry.released_on }}</small>
            <span class="changelog-meta">
              <span class="changelog-count">{{ changeCount(entry) }} 项变更</span>
              <ChevronDown class="changelog-chevron" :size="16" aria-hidden="true" />
            </span>
          </button>

          <p
            v-if="entry.summary"
            class="changelog-summary"
            :class="{ 'is-clamped': !isExpanded(entry.version) }"
          >
            {{ entry.summary }}
          </p>

          <div
            :id="bodyId(entry.version)"
            class="changelog-body"
            :aria-hidden="isExpanded(entry.version) ? 'false' : 'true'"
          >
            <div class="changelog-body-inner">
              <div v-for="section in entry.sections" :key="section.title" class="changelog-section">
                <b>{{ section.title }}</b>
                <ul>
                  <li v-for="(item, index) in section.items" :key="index">{{ item }}</li>
                </ul>
              </div>
            </div>
          </div>
        </article>
      </div>
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
  background: var(--blue-soft);
  color: var(--blue);
}
.about-copy h2,
.about-copy p,
.about-card h3,
.about-card p {
  margin: 0;
}
.about-kicker {
  margin-bottom: 6px !important;
  color: var(--blue);
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
  color: var(--ink);
  font-weight: 700;
  text-decoration: none;
}
.about-link:hover {
  color: var(--blue);
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
  gap: 12px;
}
.about-changelog-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.about-changelog-head h3 {
  margin: 0 0 4px;
  font-size: 17px;
}
.about-changelog-head p {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
}
.about-changelog-head small {
  flex: none;
  color: var(--muted);
  font-size: 12px;
}
.about-changelog-empty {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 13px;
}
.changelog-list {
  display: grid;
  gap: 10px;
}
.changelog-card {
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface);
  transition:
    border-color 0.18s ease,
    box-shadow 0.18s ease;
}
.changelog-card.is-current {
  border-left: 3px solid var(--blue);
}
.changelog-card.is-open {
  border-color: var(--blue);
  box-shadow: 0 8px 20px rgb(33 107 229 / 9%);
}
.changelog-card-head {
  display: flex;
  width: 100%;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  border: 0;
  background: transparent;
  color: var(--ink);
  cursor: pointer;
  text-align: left;
}
.changelog-card-head:hover {
  background: var(--surface-soft);
}
.changelog-card-head:focus-visible {
  outline: 2px solid var(--blue);
  outline-offset: -2px;
}
.changelog-version {
  font-size: 15px;
  font-weight: 800;
  letter-spacing: 0.01em;
  font-variant-numeric: tabular-nums;
}
.changelog-date {
  color: var(--muted);
  font-size: 12px;
}
.changelog-badge {
  flex: none;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--blue-soft);
  color: var(--blue);
  font-size: 11px;
  font-weight: 700;
}
.changelog-meta {
  display: inline-flex;
  flex: none;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}
.changelog-count {
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.changelog-chevron {
  color: var(--muted);
  transition:
    transform 0.18s ease,
    color 0.18s ease;
}
.changelog-card.is-open .changelog-chevron {
  color: var(--blue);
  transform: rotate(180deg);
}
.changelog-summary {
  margin: 0;
  padding: 0 14px 12px;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.7;
}
.changelog-summary.is-clamped {
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 1;
}
.changelog-card.is-open .changelog-summary {
  color: var(--ink);
}
.changelog-body {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows 0.22s ease;
}
.changelog-card.is-open .changelog-body {
  grid-template-rows: 1fr;
}
.changelog-body-inner {
  min-height: 0;
  overflow: hidden;
  padding: 0 14px;
}
.changelog-card.is-open .changelog-body-inner {
  padding-bottom: 14px;
}
.changelog-section + .changelog-section {
  margin-top: 12px;
}
.changelog-section b {
  display: block;
  margin-bottom: 6px;
  color: var(--blue);
  font-size: 13px;
}
.changelog-section ul {
  display: grid;
  gap: 5px;
  margin: 0;
  padding-left: 16px;
}
.changelog-section li {
  color: var(--muted);
  font-size: 13px;
  line-height: 1.7;
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
@media (max-width: 560px) {
  .changelog-card-head {
    flex-wrap: wrap;
    row-gap: 4px;
  }
  .changelog-count {
    display: none;
  }
  .about-changelog-head {
    flex-direction: column;
  }
}
@media (prefers-reduced-motion: reduce) {
  .changelog-body,
  .changelog-chevron,
  .changelog-card {
    transition: none;
  }
}
</style>
