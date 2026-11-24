<script setup lang="ts">
import { computed, ref, watch } from 'vue';

import type { SiteKind } from '../api/sites';
import { siteFaviconUrl, siteIconUrl } from '../utils/siteIcon';

const props = defineProps<{
  kind: SiteKind;
  baseUrl: string;
  name: string;
}>();

const bundledSrc = computed(() => siteIconUrl(props.kind, props.baseUrl));
const remoteSrc = computed(() => siteFaviconUrl(props.baseUrl));
const usingRemote = ref(false);
const failed = ref(false);
const src = computed(() => (usingRemote.value ? remoteSrc.value : bundledSrc.value));

// 抽屉切换站点时复用同一个组件实例，必须复位降级状态：否则上一个站的 404
// 会让下一个站跳过打包图标，直接用远端 favicon 甚至首字母占位。
watch(
  () => [props.kind, props.baseUrl],
  () => {
    usingRemote.value = false;
    failed.value = false;
  },
);

const initial = computed(() => {
  const trimmed = props.name.trim();
  return trimmed ? trimmed.charAt(0).toUpperCase() : '?';
});

const badgeStyle = computed(() => {
  let hash = 0;
  for (const char of props.kind) {
    hash = (hash * 31 + char.charCodeAt(0)) % 360;
  }
  return { background: `hsl(${hash} 48% 44%)` };
});

function handleError(): void {
  if (!usingRemote.value && remoteSrc.value && remoteSrc.value !== bundledSrc.value) {
    usingRemote.value = true;
    return;
  }
  failed.value = true;
}
</script>

<template>
  <img v-if="!failed" :src="src" :alt="name" loading="lazy" @error="handleError" />
  <span v-else class="site-icon-letter" :style="badgeStyle" :title="name" aria-hidden="true">
    {{ initial }}
  </span>
</template>

<style scoped>
.site-icon-letter {
  position: absolute;
  inset: 4px;
  display: grid;
  place-items: center;
  border-radius: 7px;
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  line-height: 1;
  user-select: none;
}
</style>
