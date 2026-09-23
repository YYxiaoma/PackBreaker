// Run the browser gate against production assets, never Vite's cold dev-module server.
// The underlying E2E script explicitly mocks every API and denies unmocked requests.
const { spawn } = require('node:child_process');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const frontend = path.join(root, 'frontend');
const origin = 'http://127.0.0.1:5173/';

function runBrowserGate() {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [path.join(__dirname, 'check-prototype.cjs')], {
      cwd: root,
      stdio: 'inherit',
    });
    child.once('error', reject);
    child.once('close', (code) => resolve(code ?? 1));
  });
}

async function waitForPreview(preview) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (preview.exitCode !== null) throw Error('生产预览服务意外退出，检查 5173 端口是否被占用');
    try {
      const response = await fetch(origin, { signal: AbortSignal.timeout(1000) });
      if (response.ok) return;
    } catch {
      // Preview is still booting. Never fall back to a real backend or external host.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw Error('生产预览服务未能在本地 5173 端口就绪');
}

async function main() {
  const preview = spawn(
    process.execPath,
    [path.join(frontend, 'node_modules/vite/bin/vite.js'), 'preview', '--host', '127.0.0.1', '--port', '5173', '--strictPort'],
    { cwd: frontend, stdio: 'inherit' },
  );
  try {
    await waitForPreview(preview);
    process.exitCode = await runBrowserGate();
  } finally {
    preview.kill('SIGTERM');
  }
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
