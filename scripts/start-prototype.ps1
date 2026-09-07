param([switch]$OpenBrowser)
$ErrorActionPreference = 'Stop'
$pbRoot = Split-Path -Parent $PSScriptRoot
$pbFrontend = Join-Path $pbRoot 'frontend'
$pbNodeCommand = Get-Command node -ErrorAction SilentlyContinue
$pbNode = if ($pbNodeCommand) { $pbNodeCommand.Source } else {
    Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
}
if (-not (Test-Path -LiteralPath $pbNode)) { throw '未找到 Node.js。请安装 Node.js 22.12+ 后重试。' }
$pbVite = Join-Path $pbFrontend 'node_modules\vite\bin\vite.js'
if (-not (Test-Path -LiteralPath $pbVite)) { throw '请先在 frontend 中运行 pnpm install --frozen-lockfile。' }
Write-Host 'PackBreaker 演示原型：http://127.0.0.1:5173/'
Write-Host '关闭本终端或按 Ctrl+C 停止。所有操作使用合成数据。'
if ($OpenBrowser) { Start-Process 'http://127.0.0.1:5173/' }
Push-Location $pbFrontend
try { & $pbNode $pbVite --host 127.0.0.1 --port 5173 --strictPort } finally { Pop-Location }
