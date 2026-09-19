// Isolated browser -> real FastAPI verification. No PT site or live client.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require('../frontend/node_modules/playwright');

const root = path.resolve(__dirname, '..');
const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), '.ci-web-approval.'));
const backendUrl = 'http://127.0.0.1:18081';
const frontendUrl = 'http://127.0.0.1:5174';
const password = 'synthetic ci browser password';
const children = [];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitReady(url) {
  for (let i = 0; i < 60; i += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch { /* Child process may not be listening yet. */ }
    await sleep(350);
  }
  throw new Error(`isolated server did not become ready: ${url}`);
}

function start(command, args, options = {}) {
  const child = spawn(command, args, {
    cwd: options.cwd || root,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, ...options.env },
  });
  let output = '';
  for (const stream of [child.stdout, child.stderr]) {
    stream.on('data', (chunk) => { output = (output + chunk.toString()).slice(-10000); });
  }
  children.push({ child, readOutput: () => output });
  return child;
}

(async () => {
  // Existing loopback services must never be treated as an isolated fixture.
  for (const url of [backendUrl, frontendUrl]) {
    let listening = false;
    try { await fetch(url, { signal: AbortSignal.timeout(500) }); listening = true; } catch {}
    assert.equal(listening, false, `refuse to touch existing service: ${url}`);
  }
  start(path.join(root, '.venv/bin/python'), [
    '-m', 'uvicorn', 'scripts.check_web_approval_fixture:app', '--host', '127.0.0.1',
    '--port', '18081', '--no-access-log',
  ], { env: { PACKBREAKER_CI_WEB_APPROVAL: '1', PACKBREAKER_CI_WEB_APPROVAL_ROOT: sandbox } });
  await waitReady(`${backendUrl}/api/v1/health/ready`);
  start('corepack', ['pnpm', 'exec', 'vite', '--config', 'vite.approval.config.ts'], {
    cwd: path.join(root, 'frontend'),
    env: { PACKBREAKER_CI_WEB_APPROVAL: '1' },
  });
  await waitReady(`${frontendUrl}/tests/web-approval.html`);

  const media = path.join(sandbox, 'data', 'web-review-source', 'Movie.2026.mkv');
  const target = path.join(sandbox, 'data', 'web-review-target');
  const before = fs.statSync(media);
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1400, height: 1000 } });
    const errors = [];
    const page = await context.newPage();
    page.on('pageerror', (error) => errors.push(error.message));
    const setup = await context.request.post(`${frontendUrl}/api/v1/auth/setup`, {
      data: { password },
    });
    assert.equal(setup.status(), 201, `synthetic setup: ${setup.status()}`);
    const login = await context.request.post(`${frontendUrl}/api/v1/auth/login`, {
      data: { username: 'admin', password },
    });
    assert.equal(login.status(), 200, `synthetic login: ${login.status()}`);
    assert.equal((await login.json()).authenticated, true);

    await page.goto(`${frontendUrl}/tests/web-approval.html?mode=full-app`);
    await page.waitForFunction(() => document.documentElement.dataset.ciApprovalReady === 'full-app');
    await page.getByRole('navigation', { name: '主导航' })
      .getByRole('button', { name: '任务中心', exact: true }).click();
    await page.getByRole('heading', { name: '任务中心' }).waitFor();
    assert.equal(decodeURIComponent(new URL(page.url()).hash.slice(1)), '任务中心');
    const definition = page.locator('.definition-table .el-table__row')
      .filter({ hasText: '隔离浏览器审批任务' });
    await definition.getByRole('button', { name: '查看', exact: true }).click();
    const definitionDrawer = page.locator('.el-drawer').filter({ hasText: '任务详情 · 隔离浏览器审批任务' });
    await definitionDrawer.getByRole('tab', { name: '执行记录' }).click();
    await definitionDrawer.locator('.execution-history-toolbar').waitFor();
    await definitionDrawer.locator('.el-table__row').getByRole('button', { name: '查看', exact: true }).click();
    const executionDrawer = page.locator('.el-drawer').filter({ hasText: '执行记录详情' });
    await executionDrawer.getByRole('tab', { name: '审核 / 对账' }).click();
    await page.getByRole('heading', { name: '审核、校验与对账' }).waitFor();
    await page.getByRole('textbox', { name: '源目录相对路径' }).fill('web-review-source');
    await page.getByRole('button', { name: '分析', exact: true }).click();
    await page.getByText('只读分析完成，已生成不可变 preflight snapshot').waitFor();
    await page.getByRole('button', { name: '刷新证据', exact: true }).click();
    const editor = page.locator('.review-editor');
    await editor.getByRole('heading', { name: '人工审核' }).waitFor();
    await editor.locator('.el-radio').filter({ hasText: 'fake' }).click();
    await editor.getByRole('button', { name: '保存审核' }).click();
    await editor.getByText(/当前 v1 · admin_session/).waitFor();
    await editor.getByRole('button', { name: '刷新执行门检查' }).click();
    await editor.getByText('ELIGIBLE', { exact: true }).waitFor();

    const selector = editor.locator('.el-form-item')
      .filter({ hasText: '目标下载器（必须已通过连接与路径安全门）' })
      .locator('.el-select');
    await selector.click();
    await page.getByRole('option', { name: /target-tr-.*Transmission/ }).click();
    await editor.locator('.el-form-item').filter({ hasText: '目标根' })
      .locator('input').fill('web-review-target');
    await editor.getByRole('button', { name: '生成无副作用计划' }).click();
    await editor.getByText('READY', { exact: true }).waitFor();
    await editor.getByText('本计划必须完整执行客户端校验，禁止 skip-check。').waitFor();

    const allTasks = await (await context.request.get(`${frontendUrl}/api/v1/tasks`)).json();
    assert.equal(allTasks.items.length, 1);
    const taskId = allTasks.items[0].id;
    const units = await (await context.request.get(`${frontendUrl}/api/v1/tasks/${taskId}/units`)).json();
    assert.equal(units.items.length, 1);
    const planResponse = await context.request.get(`${frontendUrl}/api/v1/task-units/${units.items[0].id}/execution-plan`);
    assert.equal(planResponse.status(), 200);
    const plan = await planResponse.json();
    assert.equal(plan.ready, true);
    assert.equal(plan.current, true);
    const downloaders = await (await context.request.get(`${frontendUrl}/api/v1/downloaders`)).json();
    assert.equal(downloaders.items.find((item) => item.id === plan.target_downloader_id).type, 'TRANSMISSION');
    assert.equal(plan.target_remote_save_path, '/downloads/web-review-target');
    const operations = await (await context.request.get(`${frontendUrl}/api/v1/tasks/${taskId}/operations`)).json();
    assert.deepEqual(operations.items, [], 'approval and planning must not create write journals');
    const task = await (await context.request.get(`${frontendUrl}/api/v1/tasks/${taskId}`)).json();
    assert.equal(task.status, 'AWAITING_CONFIRMATION', 'browser must not execute the plan');
    const after = fs.statSync(media);
    assert.equal(after.ino, before.ino);
    assert.equal(after.size, before.size);
    assert.equal(after.mtimeMs, before.mtimeMs);
    assert.deepEqual(fs.readdirSync(target), [], 'plan must not create hardlinks');
    assert.deepEqual(errors, [], 'real browser must not have uncaught errors');
    console.log('PASS real App navigation -> task execution record -> isolated FastAPI admin approval and Transmission read-only plan');
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  for (const child of children) console.error(child.readOutput().slice(-2200));
  process.exitCode = 1;
}).finally(async () => {
  for (const { child } of children.reverse()) {
    if (child.exitCode === null && child.signalCode === null) {
      const exited = new Promise((resolve) => child.once('exit', resolve));
      child.kill('SIGTERM');
      await Promise.race([exited, sleep(5000)]);
      if (child.exitCode === null && child.signalCode === null) {
        child.kill('SIGKILL');
        await exited;
      }
    }
  }
  // This exact path was minted by mkdtemp above and never points to user data.
  if (path.dirname(sandbox) === os.tmpdir() && path.basename(sandbox).startsWith('.ci-web-approval.')) {
    fs.rmSync(sandbox, { recursive: true, force: true });
  }
});
