# 本地开发指南

## 1. 当前状态

仓库已有 `frontend/` Vue 3 / TypeScript 交互原型与演示流程测试，后端骨架尚未创建。原型运行命令与覆盖边界见 [prototype.md](./prototype.md)。下文后端、数据库与全栈命令仍属于 M1 计划。

当前已可执行：前端 `install`、`dev`、`lint`（Prettier 格式检查）、`typecheck`、`test`、`build` 与 `test:e2e`。浏览器检查要求本地 5173 开发服务已启动，默认使用已安装 Microsoft Edge；可设置 `PB_BROWSER=chrome` 使用 Chrome。原型未对外开放真实 API，因此尚无 OpenAPI 生成客户端；`src/demo.ts` 明确限定为合成演示模型，正式接入时用生成类型替代。

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

M1 创建统一入口，README 只展示稳定命令。底层计划命令如下：

```bash
# 后端
uv sync --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy backend
uv run pytest

# 前端
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend lint
pnpm --dir frontend typecheck
pnpm --dir frontend test
pnpm --dir frontend build

# 全栈
docker compose up --build
```

应增加 `Makefile` 或 `scripts/dev`、`scripts/check`、`scripts/test` 作为稳定入口，内部调用上述工具。脚本使用严格错误处理、可重复执行，并且默认不连接真实服务。

## 5. 配置分层

优先级从高到低：测试显式覆盖 → 启动环境变量 → 数据库设置 → 代码安全默认值。

- 环境变量只用于监听、目录、主密钥位置、日志级别等启动参数。
- 站点、下载器和通知凭证通过 UI/API 写入 secret store，不进入 `.env`。
- 仓库可提供 `.env.example`，但只能包含非敏感启动变量和说明；任何值不得指向真实内网服务。
- 开发默认使用临时目录和 SQLite 临时数据库；不得复用生产 `/config` 或 `/data`。

## 6. 后端工作流

1. 从领域模型和用例测试开始，明确状态转换及失败码。
2. 实现 repository 或 adapter 端口，再接入基础设施实现。
3. 外部副作用先写 operation journal intent，测试故障注入后再接真实客户端。
4. 更新 OpenAPI，生成前端类型并检查无意的不兼容变化。
5. 运行格式、lint、mypy 和相关 pytest；安全路径改动运行完整后端测试。

异步测试使用受控 clock 和 fake adapter，禁止依赖真实等待。hash 与扫描通过受限 executor 测试，不在事件循环中直接执行重磁盘工作。

## 7. 前端工作流

1. 从 OpenAPI 生成类型和客户端，store 不重复定义服务端枚举。
2. 使用 MSW 模拟正常、空状态、加载、冲突、过期预演和错误响应。
3. 凭证组件测试不回显、不进入 URL/store/console。
4. 危险动作必须先展示服务器返回的最新影响范围，再提交确认。
5. 组件测试通过后，用 Playwright 检查桌面和移动核心流程。

## 8. 数据库变更

```bash
# 计划命令，M1 建立 Alembic 后生效
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
