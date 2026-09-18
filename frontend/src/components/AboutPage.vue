<script setup lang="ts">
import { Box, ExternalLink, GitBranch, RefreshCw, ShieldCheck } from '@lucide/vue';
import { onMounted, ref } from 'vue';

import { getSystemHealth } from '../api/system';

const emit = defineEmits<{
  'open-version': [];
}>();

const version = ref('—');

onMounted(async () => {
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
