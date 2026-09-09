# M1 安全骨架退出清单

本清单把 `docs/development-roadmap.md` 的 M1 交付项转成可重复验证的证据。M1 的代码门需要代码证据、自动测试和远端 CI 全部通过；正式里程碑切换还必须满足前置 M0 的外部语料/环境证据。真实下载器写任务仍属于 M3/M4，不因 M1 收口而提前开放。

## 前置 M0 状态

以下项目在当前仓库文档中仍明确标为待补，不能用 M1 的绿色测试替代：

- 蜂巢 3.3T 大包的脱敏目录树、torrent 结构摘要和日志，以及各类真实结构的可提交合成等价案例。
- 7 个失败样例的编号、触发条件、期望结果与人工归因。
- HHClub 的引擎、鉴权、搜索/取种方式和自动化限制。
- 首批 qBittorrent/Transmission 真实版本、NAS 文件系统、容器路径和目标目录布局矩阵。

这些属于外部验收输入，不是当前代码失败；但在补齐前不应把整体项目里程碑标记为“已正式进入 M2”。

## 后端与数据

| 条目 | 状态 | 证据 |
| --- | --- | --- |
| Python/uv、FastAPI、配置与结构化日志 | ✅ | `uv.lock`、`backend/app/server.py`、`backend/app/infrastructure/app_logging.py` |
| SQLAlchemy、SQLite WAL、Alembic、实例锁、repository | ✅ | `backend/app/infrastructure/persistence/`、`tests/integration/test_runtime.py`、`tests/integration/test_migrations.py` |
| 管理员初始化、会话、CSRF、API Token | ✅ | `tests/api/test_auth.py`、`tests/api/test_automation_access.py` |
| AES-256-GCM secret store 与统一日志脱敏 | ✅ | `tests/unit/test_security.py`、`tests/unit/test_app_logging.py`、`tests/integration/test_encrypted_store.py` |
| 任务/事件/操作日志模型与安全状态转换 | ✅ | `tests/integration/test_persistence.py`、领域状态机单元测试 |

## 前端与部署

| 条目 | 状态 | 证据 |
| --- | --- | --- |
| Vue/TypeScript/pnpm、路由、认证、生成 API 类型 | ✅ | `frontend/`、OpenAPI snapshot/type drift check |
| 下载器 CRUD、连接测试与路径映射诊断 | ✅ | `tests/api/test_downloaders.py`、前端下载器页面/测试 |
| Docker 多阶段、Compose、liveness/readiness | ✅ | `Dockerfile`、`compose.yaml`、CI `container` job |

## 安全与退出条件

| 条目 | 状态 | 自动证据 |
| --- | --- | --- |
| 格式、lint、类型、单元/集成、前端构建 | ✅ | `scripts/check.py`、`scripts/test.py`、CI `quality` |
| runtime 镜像构建与 smoke | ✅ 门禁已定义 | CI `container`；合并/发布前必须实际绿灯 |
| 浏览器核心认证 smoke | ✅ 门禁已定义 | CI `browser-e2e`；合并前必须实际绿灯 |
| 凭证 canary 不出现在日志/API/数据库明文字节 | ✅ | `tests/api/test_downloaders.py::test_downloader_canary_never_appears_in_logs_api_or_database` |
| qB/TR fake/read-only probe 基础契约 | ✅ | `tests/unit/test_downloader_adapters.py` |
| 路径穿越与映射歧义阻断 | ✅ | `tests/unit/test_downloader_paths.py`、`tests/api/test_downloaders.py` |
| 重启后配置、会话撤销与空任务队列一致 | ✅ | `tests/integration/test_restart_consistency.py` |
| 仓库敏感信息、真实 torrent/media/db/log 与大文件阻断 | ✅ | `scripts/repository_scan.py` + CI 独立扫描步骤 |

## M1 关闭判定

当前代码层面的 M1 退出条件已具备自动化证据。提交到远端后仍需同时满足两个条件，才能正式标记 M1 完成并进入 M2“解析、匹配与预演”：`quality`、`browser-e2e`、`container` 三个 GitHub Actions job 全部通过；上述 M0 外部证据完成确认并归档。

进入 M2 后继续保持以下边界：只做 torrent 解析、候选识别/匹配、piece 验证与预演；不调用下载器添加/删除/暂停/恢复等写接口，不创建生产硬链接，不修改源媒体。
