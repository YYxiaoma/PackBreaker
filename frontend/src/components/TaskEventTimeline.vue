<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';

import { ApiProblem } from '../api/client';
import { listTaskEvents, taskEventStreamUrl, type TaskEvent } from '../api/tasks';

type StreamState = 'CONNECTING' | 'LIVE' | 'FALLBACK' | 'STOPPED';

const props = withDefaults(defineProps<{ taskId: string; live?: boolean }>(), { live: true });
const emit = defineEmits<{
  changed: [event: TaskEvent];
}>();

const events = ref<TaskEvent[]>([]);
const streamState = ref<StreamState>('STOPPED');
const errorCode = ref<string | null>(null);
let eventSource: EventSource | null = null;
let fallbackTimer: ReturnType<typeof setInterval> | null = null;
let fallbackInFlight = false;

const displayEvents = computed(() => [...events.value].reverse());
const latestEventId = computed(() => events.value.at(-1)?.id);

onMounted(() => void restart());
onUnmounted(stopLiveUpdates);

watch(
  () => [props.taskId, props.live] as const,
  () => void restart(),
);

async function restart(): Promise<void> {
  stopLiveUpdates();
  events.value = [];
  errorCode.value = null;
  try {
    mergeEvents(await listTaskEvents(props.taskId, undefined, 100), false);
  } catch (error) {
    errorCode.value = error instanceof ApiProblem ? error.code : 'API_REQUEST_FAILED';
  }
  if (props.live) openEventStream();
}

function openEventStream(): void {
  if (!props.live) return;
  if (typeof EventSource === 'undefined') {
    streamState.value = 'FALLBACK';
    startFallbackPolling();
    return;
  }
  eventSource?.close();
  streamState.value = 'CONNECTING';
  const source = new EventSource(taskEventStreamUrl(props.taskId, latestEventId.value), {
    withCredentials: true,
  });
  eventSource = source;
  source.onopen = () => {
    if (eventSource !== source) return;
    streamState.value = 'LIVE';
    errorCode.value = null;
    stopFallbackPolling();
  };
  source.addEventListener('task-event', (raw) => {
    if (eventSource !== source || !(raw instanceof MessageEvent)) return;
    try {
      const event = JSON.parse(raw.data) as TaskEvent;
      if (event.task_id !== props.taskId || !event.id || !event.event_type) return;
      mergeEvents([event], true);
    } catch {
      errorCode.value = 'TASK_EVENT_PAYLOAD_INVALID';
    }
  });
  source.onerror = () => {
    if (eventSource !== source) return;
    streamState.value = 'FALLBACK';
    startFallbackPolling();
  };
}

function startFallbackPolling(): void {
  if (fallbackTimer !== null || !props.live) return;
  void pollEvents();
  fallbackTimer = setInterval(() => void pollEvents(), 2_000);
}

function stopFallbackPolling(): void {
  if (fallbackTimer !== null) clearInterval(fallbackTimer);
  fallbackTimer = null;
}

async function pollEvents(): Promise<void> {
  if (fallbackInFlight || !props.live) return;
  fallbackInFlight = true;
  try {
    const incoming = await listTaskEvents(props.taskId, latestEventId.value, 100);
    mergeEvents(incoming, true);
    errorCode.value = null;
  } catch (error) {
    errorCode.value = error instanceof ApiProblem ? error.code : 'API_REQUEST_FAILED';
  } finally {
    fallbackInFlight = false;
  }
}

function mergeEvents(incoming: TaskEvent[], notify: boolean): void {
  if (!incoming.length) return;
  const known = new Set(events.value.map((event) => event.id));
  const appended = incoming.filter((event) => !known.has(event.id));
  if (!appended.length) return;
  events.value = [...events.value, ...appended]
    .sort(
      (left, right) =>
        left.created_at.localeCompare(right.created_at) || left.id.localeCompare(right.id),
    )
    .slice(-100);
  if (notify) appended.forEach((event) => emit('changed', event));
}

function stopLiveUpdates(): void {
  eventSource?.close();
  eventSource = null;
  stopFallbackPolling();
  streamState.value = 'STOPPED';
}

function statusLabel(): string {
  if (streamState.value === 'LIVE') return 'SSE LIVE';
  if (streamState.value === 'FALLBACK') return '增量轮询';
  if (streamState.value === 'CONNECTING') return 'SSE 连接中';
  return '已停止';
}

function statusType(): 'success' | 'warning' | 'info' {
  if (streamState.value === 'LIVE') return 'success';
  if (streamState.value === 'FALLBACK') return 'warning';
  return 'info';
}

function eventSeverity(eventType: string): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  if (eventType.endsWith('_RECONCILE_REQUIRED') || eventType.endsWith('_ROLLBACK_BLOCKED')) {
    return 'danger';
  }
  if (
    eventType.endsWith('_APPLIED') ||
    eventType.endsWith('_ROLLED_BACK') ||
    eventType.endsWith('_NOOP')
  ) {
    return 'success';
  }
  if (eventType.endsWith('_INTENT_RECORDED') || eventType.endsWith('_ROLLBACK_PENDING')) {
    return 'warning';
  }
  return 'primary';
}
</script>

<template>
  <section class="task-event-timeline">
    <div class="task-event-heading">
      <div>
        <h3>真实任务时间线</h3>
        <small>仅展示持久化 TaskEvent 审计字段，不包含 checkpoint、下载器响应或凭证。</small>
      </div>
      <el-tag :type="statusType()">{{ statusLabel() }}</el-tag>
    </div>
    <el-alert
      v-if="errorCode"
      :title="`事件流暂不可用：${errorCode}`"
      description="页面会保留已加载事件；SSE 断开时自动使用当前任务的事件增量轮询。"
      type="warning"
      :closable="false"
      show-icon
    />
    <el-empty v-if="!displayEvents.length" description="暂无任务事件" :image-size="56" />
    <el-timeline v-else>
      <el-timeline-item
        v-for="event in displayEvents"
        :key="event.id"
        :timestamp="new Date(event.created_at).toLocaleString()"
        placement="top"
      >
        <div class="task-event-entry">
          <el-tag size="small" effect="plain" :type="eventSeverity(event.event_type)">
            {{ event.event_type }}
          </el-tag>
          <small>{{ event.from_status ?? '—' }} → {{ event.to_status }}</small>
          <span>{{ event.reason }}</span>
        </div>
      </el-timeline-item>
    </el-timeline>
  </section>
</template>

<style scoped>
.task-event-timeline {
  display: grid;
  gap: 12px;
  margin-top: 16px;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface);
}
.task-event-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.task-event-heading h3 {
  margin: 0 0 4px;
}
.task-event-heading small,
.task-event-entry small {
  color: var(--muted);
  font-size: 11px;
}
.task-event-entry {
  display: grid;
  gap: 4px;
}
.task-event-entry :deep(.el-tag) {
  width: fit-content;
}
@media (max-width: 720px) {
  .task-event-heading {
    flex-direction: column;
  }
}
</style>
