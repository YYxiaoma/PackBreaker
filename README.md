# PackBreaker

PackBreaker 是一个面向 PT 场景的自动拆包辅种系统。它以“大包”下载任务为输入，将内容识别为单影片或单集单元，跨站搜索匹配资源，并通过硬链接复用已有数据完成辅种。

## 核心能力

- 接入 qBittorrent 与 Transmission，支持多个下载器实例
- 监控大包任务并执行解析、搜索、匹配、硬链接、添加辅种和状态确认
- 通过统一适配器接入多个 PT 站点，首批计划支持 M-Team、HDTime 和 HHClub
- 提供失败重试、99% 卡死修复、缺失附属文件修复和人工确认
- 支持历史影片扫描辅种与电视剧按季、集拆包辅种
- 提供任务管理、配置、日志、通知和升级界面

## 技术方向

- 后端：Python 3.11、FastAPI、SQLAlchemy、Alembic、SQLite、APScheduler
- 前端：Vue 3、Vite、Element Plus、Pinia、Axios、ECharts
- 部署：Docker 单镜像，首个正式版本面向 `linux/amd64`

## 项目状态

项目已完成 M5「历史影片与电视剧」正式退出条件，当前进入 M6「发布与运维闭环」。qBittorrent 5.2.3 与 Transmission 4.1.3 均已完成真实辅种端到端主链；M-Team、HDTime、HHClub 站点适配、v1/v2/hybrid 解析与 piece 验证、人工审核、execution gate/plan、journal-backed 硬链接与下载器执行、取消/回滚/release、站点可靠性、通知、99% repair 安全链和清理/对账均已接入真实后端。M5 已完成真实 `/data` 只读增量历史扫描、HistoryScanDriver、暂停/取消/游标恢复、并发幂等 materialize、扫描级筛选与批量 Analyze/RETRY 重试，以及电视剧单集、范围集、S00/Specials、EP/ABS、Season 目录上下文和 episode group/variant 多版本归组。2026-09-14 的真实影片与真实剧集现场任务均完成 `HistoryScan → materialize → Analyze → 人工审核 → execution gate/plan → hardlink → qB DONE`；动态下载目录也真实触发 `ANALYSIS_SOURCE_CHANGED` 并在 0 journal 状态失败关闭。M4/M5 关闭证据分别见 `docs/m4-exit-checklist.md`、`docs/m5-exit-checklist.md`，M6 剩余工作见 `docs/development-roadmap.md`。

界面遵循 `PackBreaker-01-浅色控制台.png` 的设计风格，包含任务中心、预演审核、历史辅种、站点、下载器、规则、对账、日志、设置与升级页面。管理员认证、API Token、下载器、站点、任务分析/审核/执行、清理对账、通知，以及历史后台增量扫描/暂停取消/任务转换/季集归组/服务端结果筛选/批量 Analyze/RETRY 重试已接入本地 PackBreaker 后端；总览、规则以及多数 M6 发布运维能力仍包含合成示例或原型交互，主题偏好保存在浏览器。

### 体验原型

准备 Node.js 22.12+、pnpm 10.34.5：

```powershell
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend dev
```

浏览器访问 `http://127.0.0.1:5173/`。Windows 也可在依赖安装后运行 `./scripts/start-prototype.ps1`，脚本会优先使用本机 Node.js，其次使用 Codex 已提供的运行时。

安装完整开发依赖后，可用 `uv run python scripts/check.py` 执行静态检查与 OpenAPI 生成漂移检查，用 `uv run python scripts/test.py` 执行后端/前端测试和生产构建。Docker 环境可直接运行 `docker compose up --build`，默认把 PackBreaker 暴露在 `http://127.0.0.1:8000/`。

建议先审核「深空纪事」，再查看「远山回声」的映射歧义，以及第二页「森林之境」的安全修复。

原型功能范围、验证结果和待确认项见[原型体验与需求覆盖](./docs/prototype.md)。现有「核心能力」列表为产品计划，不代表已实现生产能力。

完整需求请参阅[需求基线 v0.3](./自动拆包辅种系统-需求基线-v0.3.html)。历史版本保留在[需求基线 v0.2](./自动拆包辅种系统-需求基线-v0.2.html)。

研发设计、接口规范、测试计划和实施路线请参阅[研发文档索引](./docs/README.md)。

## 许可证

本项目采用 [MIT License](./LICENSE)。
