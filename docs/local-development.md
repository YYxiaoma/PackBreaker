# 本地开发指南

## 1. 当前状态

仓库已有 `frontend/` Vue 3 / TypeScript 交互界面与演示流程测试，并已开始建立 `backend/` M1 安全骨架。当前后端能力包含任务状态转换、幂等键、qB/TR 校验安全门、FastAPI 应用入口、`X-Trace-Id` 传播、启动配置、单实例锁、`/api/v1/health/live` 与 `/api/v1/health/ready`，SQLite WAL / SQLAlchemy / Alembic 持久化与任务/操作日志 repository，管理员首次初始化、Argon2id 口令哈希、持久会话、CSRF、API Token、可信代理、安全响应头、AES-256-GCM secret store，以及下载器 CRUD、qB/TR 只读连接探测和路径映射诊断。前端已用 Axios + Pinia 接入管理员首次初始化/登录/退出、API Token 管理和下载器真实配置 API；M1 的下载器适配器仍只允许连接/能力读取，不提供任何任务写方法。其他业务页面、站点适配器与生产辅种执行仍属于后续工作。界面覆盖边界见 [prototype.md](./prototype.md)。

当前已可执行：前端 `install`、`dev`、`lint`（Prettier 格式检查）、`typecheck`、`test`、`build`、`api:types` 与 `test:e2e`。浏览器检查要求本地 5173 开发服务已启动，默认使用已安装 Microsoft Edge；可设置 `PB_BROWSER=chrome` 使用 Chrome。Vite 开发服务把 `/api` 代理到本机 8000 端口，生产部署则继续使用 FastAPI 同源入口。`src/demo.ts` 仍只服务合成任务页面；认证/API Token 已直接复用生成 OpenAPI schema 类型，下载器响应因当前服务端仍使用通用响应字典，暂时保留手写 strict view 类型。
Docker 镜像设置 `PACKBREAKER_FRONTEND_DIR=/app/frontend/dist`，FastAPI 只在该配置显式存在时服务 `/` 与 `/assets/*`；开发模式默认不设置此变量，因此 Vite 仍独立运行。镜像入口显式关闭 Uvicorn 的通用 proxy-header 解释，继续只接受应用层 `PACKBREAKER_TRUSTED_PROXIES` 白名单。

## 2. 开发环境

- Python 3.11。
- uv，用于 Python 依赖、虚拟环境和锁文件。
- Node.js 22 LTS。
- pnpm 10，通过 Corepack 固定版本。
- Docker Engine 与 Compose v2，用于集成和端到端测试。
- Git，默认分支 `main`。

版本应在 `.python-version`、`pyproject.toml`、`package.json#packageManager` 和 CI 中保持一致。升级主版本需验证构建镜像及全部测试。

## 3. 计划目录结构

```text
PackBreaker/
├── backend/
│   ├── app/
│   │   ├── api/                 FastAPI 路由和依赖
│   │   ├── application/         用例编排
│   │   ├── domain/              实体、状态机和安全规则
│   │   ├── infrastructure/
│   │   │   ├── adapters/        site/downloader/notification
│   │   │   ├── persistence/     SQLAlchemy repository
│   │   │   └── filesystem/      安全文件系统网关
│   │   └── main.py
│   └── migrations/
├── frontend/
│   └── src/
│       ├── api/                 生成客户端与薄封装
│       ├── components/
│       ├── stores/
│       ├── views/
│       └── router/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   ├── e2e/
│   └── support/                 合成 torrent/媒体生成器
├── docs/
├── scripts/
├── pyproject.toml
├── Dockerfile
└── compose.yaml
```

领域层不导入 FastAPI、SQLAlchemy 或第三方客户端。测试按能力归类，后端测试不散落在生产包中。

## 4. 计划命令

M1 使用根目录 `pyproject.toml` 与已提交的 `uv.lock` 固定 Python 依赖。推荐先安装依赖，再使用统一入口：

```bash
uv sync --frozen --all-groups
corepack enable
corepack install --global pnpm@10.34.5
corepack pnpm --dir frontend install --frozen-lockfile

# 格式/lint/类型/OpenAPI 漂移
uv run python scripts/check.py

# pytest + Vitest + production build
uv run python scripts/test.py

# 单镜像本地运行
docker compose up --build
```

`scripts/check.py` 与 `scripts/test.py` 都使用严格失败语义并默认离线，不访问真实 PT 或下载器。底层 Ruff/mypy/pytest/pnpm 命令仍可单独执行用于聚焦调试。Docker/Compose 不属于默认单元测试前置条件；没有 Docker Engine 的开发机仍可完成代码与测试门禁。

## 5. 配置分层

优先级从高到低：测试显式覆盖 → 启动环境变量 → 数据库设置 → 代码安全默认值。

- 环境变量只用于监听、目录、主密钥位置、日志级别等启动参数。
- 启动配置由 `AppSettings` 校验，应用只读取进程环境，不自动读取 `.env`。
- 站点、下载器和通知凭证通过 UI/API 写入 secret store，不进入 `.env`。
- 仓库可提供 `.env.example`，但只能包含非敏感启动变量和说明；任何值不得指向真实内网服务。
- 开发默认使用临时目录和 SQLite 临时数据库；不得复用生产 `/config` 或 `/data`。

## 6. 后端工作流

1. 从领域模型和用例测试开始，明确状态转换及失败码。
2. 实现 repository 或 adapter 端口，再接入基础设施实现。
3. 外部副作用先写 operation journal intent，测试故障注入后再接真实客户端。
4. 更新 OpenAPI，生成前端类型并检查无意的不兼容变化。
5. 运行格式、lint、mypy 和相关 pytest；安全路径改动运行完整后端测试。

OpenAPI 更新流程固定为：

```bash
uv run python scripts/export_openapi.py frontend/openapi.json
pnpm --dir frontend api:types
```

`frontend/openapi.json` 和 `frontend/src/api/generated/schema.ts` 都纳入版本控制。`tests/contract/test_openapi_snapshot.py` 会比较运行时 `app.openapi()` 与 snapshot，避免后端接口变化但前端类型未同步。

异步测试使用受控 clock 和 fake adapter，禁止依赖真实等待。hash 与扫描通过受限 executor 测试，不在事件循环中直接执行重磁盘工作。

## 7. 前端工作流

1. 从 OpenAPI 生成类型和客户端，store 不重复定义服务端枚举。
2. 使用 MSW 模拟正常、空状态、加载、冲突、过期预演和错误响应。
3. 凭证组件测试不回显、不进入 URL/store/console。
4. 危险动作必须先展示服务器返回的最新影响范围，再提交确认。
5. 组件测试通过后，用 Playwright 检查桌面和移动核心流程。

默认 `test:e2e` 会在浏览器层拦截 `/api/v1/auth/me` 为合成的已认证管理员，只验证前端导航与交互，不需要真实管理员口令或后端服务，也不会连接真实下载器。真实认证与下载器契约分别由 Vitest 和后端 API/contract 测试覆盖。

本地调试真实页面时先启动 FastAPI，再启动 Vite。首次打开界面会自动调用 `/auth/me`：未初始化时显示管理员初始化门，已初始化但无有效会话时显示登录门；无需再通过 Swagger 手工建立会话。浏览器始终请求同源 `/api/v1`，因此 CSRF Cookie/请求头语义与生产入口一致。只有用户主动点击下载器“测试连接”或“运行真实诊断”时才会访问已配置下载器或服务端文件系统；默认 Vitest/pytest 不连接真实服务。

## 8. 数据库变更

```bash
# 已建立 Alembic；使用专用开发数据库，避免误用生产数据
mkdir -p runtime
export PACKBREAKER_DATABASE_URL="sqlite+pysqlite:///./runtime/dev.db"
uv run alembic revision --autogenerate -m "说明"
uv run alembic upgrade head
uv run alembic check
```

自动生成迁移后必须人工检查约束、默认值、索引和数据回填。提交中同时包含 ORM、迁移、repository 测试和文档变化。

## 9. 测试数据

- 使用固定随机种子在临时目录生成媒体字节与 torrent，不提交生成产物。
- fixture 名称明确表达场景，例如 `v1_cross_file_piece`、`ambiguous_same_basename`。
- 真实问题先提取结构特征，再构造最小合成案例；不得直接复制真实 torrent 或日志。
- 需要人工验证真实服务时，使用单独标记和环境开关，默认测试命令排除。

## 10. 提交前检查

- 变更符合 v0.3 和 AGENTS.md，没有绕过安全门。
- 没有 `.env`、secret、torrent、数据库、日志或媒体文件进入暂存区。
- 格式、静态检查、相关测试和文档链接通过。
- 新增错误码、状态、API 或配置已同步文档和前端类型。
- 提交使用 Conventional Commits，保持单一目的。
