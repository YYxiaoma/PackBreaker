import { createApp, h } from 'vue';
import ElementPlus from 'element-plus';
import { createPinia } from 'pinia';
import 'element-plus/dist/index.css';
import 'element-plus/theme-chalk/dark/css-vars.css';

import { getTask, listTasks } from '../src/api/tasks';
import App from '../src/App.vue';
import TaskExecutionEvidencePanel from '../src/components/TaskExecutionEvidencePanel.vue';
import '../src/style.css';
import '../src/details.css';
import '../src/module-card-theme.css';

// The real review/analysis components talk to the real isolated FastAPI
// server; only the test-only mount skips unrelated navigation/screens.
async function main(): Promise<void> {
  if (new URLSearchParams(location.search).get('mode') === 'full-app') {
    // Mount exactly the production root with production Pinia/Element Plus.
    // Only the loopback fixture and Vite proxy differ from deployed runtime.
    createApp(App).use(createPinia()).use(ElementPlus).mount('#app');
    document.documentElement.dataset.ciApprovalReady = 'full-app';
    return;
  }
  const tasks = await listTasks();
  if (tasks.length !== 1) throw new Error('CI must own exactly one synthetic task');
  const task = await getTask(tasks[0].id);
  if (task.type !== 'PACKAGE_UNPACK') throw new Error('unexpected task type');
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
