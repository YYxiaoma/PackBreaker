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

项目当前已完成 M1 安全骨架，并进入 M2「解析、匹配与预演」代码级收口。后端已经具备安全 bencode 与 v1/v2/hybrid torrent 解析、TaskUnit 识别、媒体 token 规范化、候选排序、M-Team/HDTime 只读站点适配、唯一文件映射、流式 piece 验证、验证缓存、不可变 preflight、版本化人工审核与重验证、pre-execution gate，以及无副作用 `execution-plan` 预览。执行计划会重新绑定 current+eligible gate、metainfo digest、源 inventory 和目标树状态，生成 HARDLINK/CLIENT_FETCH/PADDING/ZERO_LENGTH 动作并在目标冲突、父目录异常或跨设备时失败关闭；当前响应始终保持 `execution_allowed=false`、`side_effects_started=false`，不会进入 LINKING 或调用下载器写接口。前端任务中心、真实 Analyze、预演审核、人工映射、重验证、执行门与执行计划预览均已接入真实后端；M2 代码门与外部验收缺口见 `docs/m2-exit-checklist.md`。

界面遵循 `PackBreaker-01-浅色控制台.png` 的设计风格，包含任务中心、预演审核、历史扫描、站点、下载器、规则、对账、日志、设置与升级页面。管理员认证、API Token、下载器、站点配置以及 M2 任务分析/审核主流程已经接入本地 PackBreaker 后端；总览、规则、历史扫描、对账和多数 M4-M6 运维能力仍包含合成示例或原型交互，主题偏好保存在浏览器。

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
