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

项目当前已完成 M1 安全骨架的代码级收口：仓库包含后端领域安全规则、启动配置、单实例执行锁、FastAPI 存活/就绪检查、SQLite WAL / SQLAlchemy 持久化、Alembic 迁移、任务/操作日志 repository、管理员首次初始化与持久会话/CSRF、API Token scope/过期/撤销、可信代理与安全响应头、AES-256-GCM secret store、JSON 结构化日志与统一敏感字段脱敏，以及下载器配置 CRUD、qBittorrent/Transmission 只读连接与能力探测、路径映射/硬链接可行性诊断。前端已通过 Axios + Pinia 接入管理员初始化/登录/退出、API Token 管理和下载器真实配置 API，写操作使用 CSRF 与 `If-Match`；OpenAPI schema 与生成 TypeScript 类型纳入仓库漂移检查。M1 部署骨架包含 `uv.lock`、Docker 多阶段单镜像、Compose、同源前端静态资源、统一 `scripts/check.py` / `scripts/test.py`、仓库敏感信息/大文件扫描和 GitHub Actions 质量/浏览器/容器门禁。M1 代码门逐项证据见 `docs/m1-exit-checklist.md`；正式进入 M2 还需远端三条 CI job 全绿，并补齐 M0 中仍待确认的真实语料、失败样例归因、HHClub 信息与真实下载器/NAS 版本矩阵。当前下载器适配器仍不暴露添加、删除、暂停、恢复、校验等任务写操作，真实站点接入、生产文件创建/修复与辅种执行仍未实现。

界面遵循 `PackBreaker-01-浅色控制台.png` 的设计风格，包含任务中心、预演审核、历史扫描、站点、下载器、规则、对账、日志、设置与升级页面。管理员认证、API Token 与“下载器”管理已经接入本地 PackBreaker 后端；任务、站点、规则和多数运维页面目前仍使用合成示例，主题偏好保存在浏览器。

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
