import { createApp, h } from 'vue';
import ElementPlus from 'element-plus';
import 'element-plus/dist/index.css';

import { getTask, listTasks } from '../src/api/tasks';
import TaskExecutionEvidencePanel from '../src/components/TaskExecutionEvidencePanel.vue';

// The real review/analysis components talk to the real isolated FastAPI
// server; only the test-only mount skips unrelated navigation/screens.
async function main(): Promise<void> {
  const tasks = await listTasks();
  if (tasks.length !== 1) throw new Error('CI must own exactly one synthetic task');
  const task = await getTask(tasks[0].id);
  if (task.source_hash !== 'synthetic-source-hash') throw new Error('unexpected task source');
  const app = createApp({
    setup: () => () => h(TaskExecutionEvidencePanel, { taskId: task.id }),
  });
  app.use(ElementPlus);
  app.mount('#app');
  document.documentElement.dataset.ciApprovalReady = 'true';
}

void main().catch((error: unknown) => {
  document.documentElement.dataset.ciApprovalError =
    error instanceof Error ? error.message : 'unknown error';
});
