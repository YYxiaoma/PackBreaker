# AGENTS.md

本文件适用于整个 PackBreaker 仓库，是自动化编码代理、代码生成工具和协作开发者必须遵守的仓库级规则。

目标不是“尽快让代码跑起来”，而是**在不破坏源数据、不泄漏凭证、不绕过安全门的前提下持续推进功能**。

---

## 1. 沟通与事实口径

- 与用户沟通、界面文案、说明文档和提交说明优先使用中文。
- 代码标识符、协议字段、第三方 API 原始名称保持英文。
- 必须明确区分：
  - **已实现**：代码与测试已经存在；
  - **已验证**：已有自动化或真实环境证据；
  - **计划实现 / PENDING**：只有设计或 Registry 占位；
  - **外部阻塞**：需要真实站点、Provider、Bot、Docker daemon 等外部条件。
- 不得把 MockTransport、Fake backend 或设计文档描述成真实环境证据。
- 不得为了让测试通过而弱化安全断言、删除失败场景、跳过迁移验证或扩大默认权限。

---

## 2. 研发约束的优先级

发生冲突时按以下顺序判断：

1. `docs/README.md` 中当前版本设计、支持矩阵、已知限制、测试与验收文档；
2. 已落地的领域安全不变量与自动化测试；
3. 本文件；
4. README 产品介绍。

README 可以有宣传表达，但**不能作为降低安全门或扩大正式支持范围的依据**。

历史需求基线 HTML 和早期原型图片已从当前工作树移除，需要追溯时使用 Git 历史。

---

## 3. 当前仓库结构

```text
backend/                     FastAPI、领域服务、适配器、持久化、迁移
backend/migrations/          Alembic 历史迁移链
frontend/                    Vue 3 管理端
tests/                       单元 / API / Application / Integration 测试
docs/                        设计、支持矩阵、验收、部署和发布文档
scripts/                     check / test / 发布 / 运维 / E2E 工具
release-baseline.json        上一正式 Release 的不可变升级基线
frontend/openapi.json        受版本控制的 OpenAPI 快照
frontend/src/api/generated/  OpenAPI 生成的 TypeScript 类型
```

技术基线：

- Python 3.11
- FastAPI / SQLAlchemy / Alembic / SQLite WAL
- Vue 3 / Vite / TypeScript strict / Element Plus / Pinia
- pytest / Ruff / mypy / Vitest / Playwright
- Docker 单镜像；当前正式 v0.1.9 仅发布 `linux/amd64`，v1.0.0 待验收目标为 `linux/amd64` + `linux/arm64`

---

## 4. 不可破坏的核心安全不变量

### 4.1 源数据只读

- 源媒体数据默认视为只读。
- 自动流程不得截断、覆盖、重命名或删除源文件。
- Hardlink 目标如果需要被客户端下载器写入修复，必须先隔离为独立 inode。
- 任何实现都不能因为“目标文件看起来是 PackBreaker 创建的”就猜测 ownership；必须有 journal / snapshot 证据。

### 4.2 候选验证不能偷懒

- 名称、总大小、IMDb/豆瓣 ID、文件名相似度、抽样 hash 只能用于筛选和排序。
- 只有满足当前领域规则的 `FULL_VERIFIED` 才能进入允许跳过客户端校验的路径。
- qBittorrent 是否能 skip checking 还必须受实际 WebAPI capability 约束。
- Transmission 不得因为已有 piece 证据就跳过自身 verify 流程。
- 无法证明时应保持人工确认、客户端下载校验、阻断或 `RECONCILE_REQUIRED`，不能“猜成功”。

### 4.3 路径与文件系统必须失败关闭

必须拒绝：

- 绝对路径
- `..` 路径穿越
- NUL
- 越过允许根目录
- 不受信符号链接
- 不可证明 ownership 的目标覆盖
- 跨设备 Hardlink

文件系统副作用必须有 preflight、snapshot、journal 与幂等恢复证据。

### 4.4 所有外部写操作都必须可重放、可恢复

以下动作必须进入 operation journal 或对应的持久化幂等链：

- 创建目录
- 创建 / 删除 Hardlink
- qB add / recheck / start / remove
- Transmission add / verify / start / remove
- repair 写入
- rollback / release

要求：

- **先记录意图，再执行副作用，再记录结果**；
- 响应丢失时先读真实状态对账，不盲目重发；
- 重启后能够从已有 journal 继续收敛；
- 回滚只处理能证明由 PackBreaker 创建且 after snapshot 仍匹配的资源。

---

## 5. 凭证与隐私

### 5.1 SecretStore

以下秘密必须进入 SecretStore 或专用强哈希，不得明文落库：

- 站点 Cookie / API Key
- 下载器密码 / API Key
- 代理密码
- Telegram Bot Token
- Server酱 SendKey
- AI Provider API Key
- 管理员密码

GET API 只能返回 `credential_configured` 等布尔状态，不得回显秘密。

### 5.2 日志与诊断

日志、错误、通知、诊断包、AI Tool 上下文、测试快照中不得出现：

- Cookie
- API Key
- Token
- 密码
- announce URL query
- 带 userinfo 的 URL
- 真实 torrent payload
- 第三方原始敏感响应

一次性管理员临时密码是特殊启动凭证：

- 只允许通过专用 bootstrap 输出到 stderr；
- 不进入结构化 logging sink；
- 不写数据库；
- 不进入通知、诊断或 AI 上下文。

---

## 6. 站点开发规则

- 站点请求目标必须来自受审查的 `SiteProfileRegistry`。
- 不允许用户输入任意 URL 决定 Cookie / API Key 的发送目标。
- 当前正式持久化支持范围只由 `PERSISTED_SITE_KINDS` 决定。
- `PENDING_ADAPTER` 站点可以在 Registry / UI 中展示，但必须：
  - 前端禁用保存/测试；
  - 后端返回稳定的 pending 错误；
  - 不创建 Site；
  - 不创建 Secret。
- 新站点从 pending 升级为正式支持前，至少需要：
  1. 认证方式确认；
  2. adapter；
  3. 共享契约测试；
  4. 错误分类；
  5. 凭证同源保护；
  6. 真实只读验收。
- 不得通过自动 Headless Browser、验证码绕过或 JS Challenge 绕过扩大兼容范围。
- torrent 下载 token / 一次性下载链接默认不得做隐式自动重试。

---

## 7. 下载器开发规则

qBittorrent 与 Transmission 通过统一领域接口接入，但**不能抹平协议差异**。

### qBittorrent

- 能力判断必须基于真实 WebAPI version。
- add / recheck / start / remove 必须验证 hash、save path、ownership/tag、progress/state。
- remove 必须固定不删除数据。
- skip checking 只能在领域验证和 WebAPI capability 同时允许时使用。

### Transmission

- 4.1.x 使用 JSON-RPC 2.0 snake_case 方法。
- add 默认 paused。
- verify 与 start 必须是独立 journal 动作。
- remove 必须固定 `delete_local_data=false`。
- 不得把 qB 的 skip-check 语义套到 Transmission。

路径映射变化会使旧执行计划 stale，不能在 ADDING 阶段偷偷切换 downloader / save path。

---

## 8. 后端开发规范

- Python 使用完整类型标注。
- 领域规则放在 domain / application 层，API router 保持薄。
- 外部网络、下载器、通知、AI Provider 使用 async I/O。
- 所有外部调用必须有明确 timeout；重试只能用于明确幂等且安全的读取。
- 取消必须传播，不能吞掉 `CancelledError`。
- 稳定业务错误使用机器可读 error code；用户响应不得暴露第三方正文、堆栈或秘密。
- 数据库时间统一使用 UTC，展示层再应用用户时区。
- 数据库结构变更必须有 Alembic migration，不能只改 ORM。
- SQLite migration 特别注意父表 rebuild 与 child FK / `ON DELETE` 副作用；已有真实数据兼容优先于“迁移看起来更漂亮”。

---

## 9. 前端开发规范

- 前端资源类型优先使用生成的 OpenAPI TypeScript 类型，不重复手写可能漂移的 schema。
- 修改后端 API 后必须同步：
  1. `frontend/openapi.json`
  2. `frontend/src/api/generated/schema.ts`
  3. 前端 API wrapper / store / component
  4. 契约测试
- 新页面必须检查：
  - 桌面布局
  - 390px 左右移动视口
  - 浅色主题
  - 深色主题
  - loading / empty / error 状态
- 危险操作必须展示真实影响范围并要求明确确认。
- 凭证输入只能“写入 / 替换 / 清除”，不能回显。
- 前端不能把旧状态伪装成实时状态；实时指标失败时显示不可用或最后成功时间。

---

## 10. 认证、通知与 AI

### 管理员认证

- 正式登录为 username + password。
- 首次 bootstrap 后若是临时密码账户，只允许改密和退出等必要动作。
- 改密后撤销全部会话并要求重新登录。
- 不能重新引入未认证 setup 后门。

### 通知

- Telegram / Server酱凭证进入 SecretStore。
- 渠道连接关键字段变化后必须回到 `UNTESTED` 并停用，不能沿用旧连接证据。
- 未保存表单 probe 不能创建 Channel 或 Secret。
- 通知事件过滤在服务端执行，不能只靠前端隐藏。

### AI Agent

v0.1.6 的 AI 必须保持**只读**：

- 支持 OpenAI 与 OpenAI-compatible；
- Model 由用户自由填写；
- Provider API Key 进入 SecretStore；
- Tool 使用显式白名单；
- 禁止任意 Shell、任意 SQL、任意 URL；
- Tool 输出继续脱敏；
- 有限工具轮次与上下文上限；
- Telegram 入口必须经过 Chat ID / User ID allowlist；
- update_id cursor 必须持久化，避免重启重复消费；
- 未授权 Telegram 更新不能触发 Provider 或 Tool；
- 不提供 Web AI Chat；
- AI 不得新增绕过 CSRF、Idempotency-Key、人工审核、Execution Gate 或 journal 的写接口。

---

## 11. 数据库与迁移

- `pyproject.toml` 是当前项目版本主来源。
- 新 migration 必须接到当前 Alembic head。
- 历史 migration 不做重写式“美化”。
- 每次 schema 变化至少验证：
  - fresh DB → head；
  - 上一正式版本 → head；
  - migration 数据保真；
  - Runtime 自动升级；
  - 备份/恢复兼容。
- 对真实生产数据库做研发验收时，默认：
  1. SQLite backup API 创建一致性副本；
  2. 只迁移副本；
  3. 原库只读；
  4. 验收结束再次确认原 revision 未变化。

除非用户明确授权，不得拿真实运行库直接做开发迁移试验。

---

## 12. 版本、OpenAPI 与发布基线

### 当前版本表面必须一致

修改版本时同步检查：

- `pyproject.toml`
- `frontend/package.json`
- Dockerfile `ARG VERSION`
- `uv.lock` root package version
- OpenAPI `info.version`
- runtime `app_version()`

### release-baseline.json 的语义

`release-baseline.json` **不是当前 candidate 版本**。

它必须固定“上一正式已发布 Release”的：

- version / tag
- commit
- immutable image digest
- Alembic revision
- release workflow run id

它用于 candidate 的相邻版本升级和回滚门禁。不要为了“版本号看起来一致”把 baseline 改成当前未发布 candidate。

### OpenAPI 生成

标准流程：

```bash
python scripts/export_openapi.py frontend/openapi.json
frontend/node_modules/.bin/openapi-typescript \
  frontend/openapi.json \
  -o frontend/src/api/generated/schema.ts
frontend/node_modules/.bin/prettier \
  --config frontend/.prettierrc.json \
  --write frontend/src/api/generated/schema.ts
```

不要对 `frontend/openapi.json` 额外跑 Prettier；仓库漂移门使用 exporter 的规范输出。

---

## 13. 测试规则

普通自动化默认**完全离线**：

- 不访问真实 PT；
- 不访问真实 qB / Transmission；
- 不读取真实媒体；
- 不发送真实 Telegram；
- 不调用真实 AI Provider；
- 不操作 Docker daemon，除非明确在专用 Docker CI Job。

修复缺陷时必须增加“修复前失败、修复后通过”的回归测试。

高风险改动需要重点覆盖：

- BitTorrent v1/v2/hybrid
- 路径逃逸
- inode / source snapshot
- operation journal
- 并发幂等
- 响应丢失
- 进程重启
- rollback
- migration
- Secret canary

标准本地门禁：

```bash
python scripts/check.py
python scripts/test.py
```

其中 `scripts/check.py` 是提交前 canonical 静态门；修改生成契约、迁移、安全边界、发布逻辑时必须通过。

浏览器交互变化还需要运行：

```bash
node scripts/check-prototype.cjs
```

该 E2E 的未显式 mock API 必须继续失败关闭。

---

## 14. 真实环境验收

真实环境只用于自动化无法替代的现场证据。

原则：

- 默认只读；
- 先明确会发出哪些请求；
- 不主动搜索/取种/写下载器，除非验收目标明确要求并得到用户授权；
- 不输出 Cookie / API Key / UID / username / 内网地址；
- 真实站点失败不能通过切镜像域名或降低校验“修成通过”；
- 外部站点故障、缺凭证、无 Docker daemon 必须明确记为阻塞，不伪造完成。

现场证据应写入专门验收文档，并只保留非敏感摘要。

---

## 15. Git 与仓库卫生

- 使用 Conventional Commits，例如：
  - `feat: add downloader runtime metrics`
  - `fix: preserve source inode during repair`
  - `chore: refresh release baseline`
- 一个提交尽量保持一个清晰主题。
- 未经用户明确要求，不擅自 commit / push / tag / release。
- 不 force push，除非用户明确要求并已说明风险。
- 不提交：
  - 真实 Cookie / API Key / Token / 密码
  - `.torrent`
  - SQLite 数据库
  - runtime 日志
  - 媒体文件
  - secret.key
  - 本地缓存 / 临时测试产物
- 新增依赖前检查必要性、维护状态、许可证和锁文件。
- 删除历史文件时同步清理 README / docs / AGENTS 中的引用。

---

## 16. 完成一个任务前

至少确认：

1. 功能是否真的实现，而不是只改 UI；
2. 安全门有没有被绕过；
3. 是否需要 migration；
4. OpenAPI / 生成类型是否同步；
5. 是否需要更新支持矩阵 / 已知限制 / 当前版本设计；
6. 是否添加了回归测试；
7. 是否通过与改动范围匹配的静态和动态测试；
8. 是否存在真实环境才能证明的部分；
9. 是否误把外部阻塞描述成成功；
10. 工作区是否混入秘密或临时产物。

完成说明必须列出：**改了什么、验证了什么、还没验证什么、是否存在外部条件。**
