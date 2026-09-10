# API 设计

## 1. 基本约定

- API 前缀：`/api/v1`。
- 请求与响应：UTF-8 JSON；文件下载端点除外。
- 标识符：UUID 字符串。
- 时间：RFC 3339 UTC，例如 `2026-09-05T08:30:00Z`。
- 枚举：使用领域文档中定义的大写稳定值。
- 字段命名：JSON 使用 `snake_case`，TypeScript 客户端由 OpenAPI 生成或统一映射。
- OpenAPI：`/api/openapi.json`；开发环境开放交互文档，生产环境仅管理员可访问。
- 版本只在出现不兼容变更时升级；新增可选字段和端点保持 v1 兼容。

成功响应直接返回资源或列表结构，不套无意义的 `data` 包装。列表统一返回：

```json
{
  "items": [],
  "next_cursor": null
}
```

## 2. 错误格式

错误使用 `application/problem+json`，结构遵循 RFC 9457 并扩展稳定错误码：

```json
{
  "type": "https://packbreaker.dev/problems/path-mapping-invalid",
  "title": "路径映射无效",
  "status": 422,
  "detail": "下载器路径无法映射到容器内允许目录",
  "code": "PATH_MAPPING_INVALID",
  "trace_id": "7aa9d6f7-61f3-4a75-a1ec-6210291163e1",
  "errors": [
    {"field": "path_mappings.0.container_path", "reason": "not_accessible"}
  ]
}
```

API 和前端只依赖 `code` 进行分支处理，不解析 `detail` 文本。错误详情经过脱敏，不返回堆栈、绝对源路径或第三方原始响应正文。

## 3. 认证与会话

### 3.1 管理界面

- 首次启动且不存在管理员时，`POST /auth/setup` 设置单管理员口令；完成后该端点永久返回 409。
- `POST /auth/login` 成功后设置 `HttpOnly`、`SameSite=Strict` 会话 Cookie；有效 HTTPS 请求同时设置 `Secure`。独立 CSRF Cookie 可由前端读取，但数据库只保存两个随机 token 的摘要。
- `setup`/`login` 属于认证前入口；其余由管理会话保护的非 GET/HEAD 请求同时校验 CSRF Cookie 与 `X-CSRF-Token` 请求头。
- `POST /auth/logout` 在 CSRF 校验后持久化撤销当前会话；`GET /auth/me` 返回 `configured`、当前会话状态和权限，使前端可区分“尚未初始化管理员”与“已初始化但未登录”，不返回口令信息。会话明文 token 不入库。
- 登录失败按来源摘要和单管理员账号双维度限速，不记录口令。

### 3.2 自动化 API

- 管理员可通过受 CSRF 保护的管理会话生成具备名称、范围和过期时间的 API Token；创建响应只显示一次 `pbk_` 前缀明文，数据库仅保存 SHA-256 摘要。
- 使用 `Authorization: Bearer <token>`；范围首版包含 `tasks:read`、`tasks:write`、`config:read`、`config:write`。范围不足返回 `403 API_TOKEN_SCOPE_FORBIDDEN`，过期或已撤销返回 `401 API_TOKEN_INVALID`。
- `GET /api-tokens` 只返回元数据，不返回明文或摘要；`DELETE /api-tokens/{id}` 持久化撤销且重复撤销幂等。
- `/system/status` 允许管理员会话或具备 `config:read` 的 API Token 访问，用于统一认证/授权链路的首个只读端点。
- Webhook 不使用管理会话或 API Token，按第 8 节独立验签。

## 4. 幂等、并发与分页

- 创建任务、执行任务动作、创建历史扫描等副作用请求必须带 `Idempotency-Key`。
- 服务端保存 key、调用方身份、请求摘要和响应结果；同 key 同请求返回原结果，同 key 不同请求返回 `409 IDEMPOTENCY_CONFLICT`。
- 站点、下载器、规则等可编辑资源包含整数 `version`；更新和删除使用 `If-Match: "<version>"`，版本不一致返回 412。
- 列表使用不透明 cursor，默认 50 条、最大 200 条；排序字段和 cursor 绑定，禁止混用。
- 所有响应返回 `X-Trace-Id`；调用方可传 `X-Trace-Id`，格式非法时由服务端重新生成。

## 5. 资源端点

### 5.1 系统与认证

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health/live` | 进程存活，不检查外部依赖 |
| GET | `/health/ready` | 数据库、迁移和 worker 就绪状态 |
| GET | `/system/status` | 版本、任务统计和脱敏依赖状态 |
| POST | `/auth/setup` | 首次设置管理员口令 |
| POST | `/auth/login` | 创建管理会话 |
| POST | `/auth/logout` | 注销当前会话 |
| GET | `/auth/me` | 管理员是否已初始化、当前会话与权限信息 |
| GET/POST/DELETE | `/api-tokens` | 管理自动化 Token；列表不返回明文 |

### 5.2 站点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/sites` | 列表、创建站点 |
| GET/PATCH/DELETE | `/sites/{site_id}` | 详情、局部更新、删除未被任务引用的站点 |
| POST | `/sites/{site_id}/test` | 测试鉴权、搜索和取种能力，不持久化原始响应 |
| GET | `/sites/{site_id}/health` | 最近状态、熔断和限流信息 |
| POST | `/sites/{site_id}/actions` | `enable`、`disable`、`reset_circuit` |

凭证字段为只写对象。读取时只返回 `credential_configured` 和 `credential_updated_at`；传 `null` 表示保持不变，显式 `clear_credential` 才能删除。

### 5.3 下载器

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/downloaders` | 列表、创建下载器 |
| GET/PATCH/DELETE | `/downloaders/{downloader_id}` | 详情、更新、删除未被任务引用的下载器 |
| POST | `/downloaders/{downloader_id}/test` | 测试版本、认证和能力 |
| POST | `/downloaders/{downloader_id}/path-diagnostics` | 验证下载器路径到容器路径映射 |
| GET | `/downloaders/{downloader_id}/tasks` | 只读查询下载器任务摘要 |
| POST | `/downloaders/{downloader_id}/actions` | `enable`、`disable`、`refresh_capabilities` |

下载器读取端点允许管理员会话或 `config:read` API Token；创建、更新、删除、连接测试、路径诊断和 action 允许受 CSRF 保护的管理员会话或 `config:write` API Token。PATCH、DELETE、enable/disable 使用 `If-Match: "<version>"`，缺失返回 428，版本冲突返回 412。凭证对象为只写字段：创建/更新时可提交 qB 用户名+密码、qB API Key 或 Transmission 用户名+密码；读取只返回 `credential_configured`。PATCH 中 `credential: null` 表示保持原凭证，只有 `clear_credential: true` 才清除。

路径映射采用最长前缀规则；当前 M1 以 `PACKBREAKER_DATA_DIR` 作为允许根目录，后续配置层可进一步收窄 source roots。诊断请求的 `probes` 数组提交一组或多组“下载器视角的已存在测试文件 + 容器内目标目录”；响应逐项返回规则命中、容器可见性、设备 ID、文件类型、读写权限、双向映射和硬链接可行性，并返回 `all_mappings_verified`。只有每条配置映射都至少被一个成功 probe 覆盖时全局路径状态才为 `OK`，否则保持阻断，不能 enable。诊断允许创建并立即删除目标目录中的临时 hardlink 以验证内核能力，但不修改源文件字节；解析到允许根目录外的符号链接直接拒绝。

### 5.4 站点配置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/sites` | 列表；创建站点配置，凭证只写 |
| GET/PATCH/DELETE | `/sites/{site_id}` | 读取、并发安全修改或删除；写操作要求强 `If-Match` |
| POST | `/sites/{site_id}/test` | 使用 SecretStore 中的站点凭证执行只读连接测试 |
| POST | `/sites/{site_id}/actions` | `enable`、`disable`、`refresh_capabilities` |

站点凭证统一为只写 `credential` 对象：`MTEAM` 要求 `API_KEY`，`HDTIME` 要求 `COOKIE`。读取只返回 `credential_kind` 与 `credential_configured`，永不返回凭证值或 `secret_id`；`clear_credential: true` 才会显式删除。修改站点地址、类型或凭证会清空旧 capability、重置连接状态并自动禁用；切换站点类型必须同时提交新类型的地址，旧凭证存在时还必须替换或清除，避免跨类型复用秘密。启用前必须已有对应类型凭证且最近一次只读连接测试成功。

### 5.5 拆包任务

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/tasks` | 筛选任务；手动创建任务 |
| GET | `/tasks/{task_id}` | 任务摘要、当前状态、进度和错误 |
| GET | `/tasks/{task_id}/timeline` | 状态事件和脱敏操作摘要 |
| GET | `/tasks/{task_id}/units` | 处理单元与当前决策 |
| GET | `/tasks/{task_id}/candidates` | 候选、评分、硬约束和验证状态 |
| GET | `/tasks/{task_id}/preflight` | 最近一次不可变预演快照 |
| GET | `/tasks/{task_id}/preflight/current` | 轻量判断最近预演是否仍匹配 task/source/site 输入 |
| POST | `/tasks/{task_id}/actions` | M2 当前实现 `analyze`；后续扩展 `pause`、`resume`、`retry`、`cancel`、`reconcile` |
| GET/POST | `/task-units/{unit_id}/decision` | 读取/追加版本化人工审核 revision；批准/拒绝候选或提交人工文件映射 |
| GET | `/task-units/{unit_id}/decision/verification` | 读取当前审核 revision 的不可变重验证证据 |
| POST | `/task-units/{unit_id}/decision/actions` | M2 当前实现 `reverify`；重新获取同一 torrent 并验证人工映射 |
| GET/POST | `/task-units/{unit_id}/execution-gate` | 读取/刷新不可变 pre-execution gate；仅判断是否可进入后续安全准备，不启动副作用 |

`POST /tasks` 只登记任务身份并复用现有幂等键；重复的 task type、来源下载器、source hash 与 normalized unit key 组合返回同一任务，不会触发扫描、站点搜索或下载器写操作。M2 的手动 `analyze` 当前同步执行，只接受相对于服务端 `/data` 的 `source_root`；绝对路径、`..`、Windows drive、反斜杠和任意符号链接路径都会拒绝。Analyze 只允许从 `PENDING`、`RETRY` 或 `PAUSED` 开始，并通过短事务依次记录 `ANALYZING → SEARCHING → MATCHING → VERIFYING → PREFLIGHT`；站点请求、torrent 解析、文件扫描和 piece 哈希均不在数据库事务中执行。分析失败时仅在任务 version 仍由本次运行持有的情况下安全回到 `RETRY`。分析会持久化当前 inventory 下识别的 TaskUnit、最新 preflight 对应的 Candidate 证据和不可变 snapshot；snapshot 绑定进入 `PREFLIGHT` 后的最终 task version。`GET /preflight` 同时返回 `current` 与 `stale_reasons`；历史 snapshot 永不因过期而原位修改。人工审核 revision 每次保存完整当前状态，使用 `expected_version` 做乐观并发；只能引用当前 preflight 的 Candidate，硬冲突候选不能被人工批准绕过。人工映射首版只允许从当前 `AMBIGUOUS` 项的候选源文件集合中选择，并会标记 `requires_reverification=true`。`reverify` 会重新确认 preflight/current、source inventory、审核 revision 与站点身份，重新获取同一 torrent 并核对 metainfo digest，再应用人工映射执行 v1/v2/hybrid 内容验证；结果以只追加 `task_review_verification` 证据保存，不修改原 preflight/candidate 证据。首个有效 revision 仅允许从 `PREFLIGHT` 通过 `REVIEW_OPENED` 进入 `AWAITING_CONFIRMATION`；该唯一状态 bridge 不使同一 preflight 自身失效，其他 task version 变化仍会使其 stale。`execution-gate` 将 task/preflight/review/candidate/可选重验证证据绑定成稳定 digest；`FULL_VERIFIED` 可获得进入后续准备阶段的资格，`CLIENT_CHECK_REQUIRED` 也可获得资格但必须携带 `client_check_required=true`，`BLOCKED`、hard reject、stale、缺少批准候选或缺少必要重验证证据均失败关闭。Gate 本身只追加证据，响应固定 `side_effects_started=false`，不会改变任务状态、进入 LINKING、创建目录/硬链接或调用下载器写接口。后续通用任务动作仍按异步 202/action ID 设计；取消已进入下载器的任务时，请求必须带 `remove_downloader_task` 与 `rollback_created_resources` 明确选择。

### 5.5 历史扫描与修复

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/history-jobs` | 列表、创建历史扫描 |
| GET | `/history-jobs/{job_id}` | 游标、统计和错误 |
| POST | `/history-jobs/{job_id}/actions` | `pause`、`resume`、`cancel`、`retry_failed` |
| GET/POST | `/repair-jobs` | 列表、根据任务创建修复作业 |
| GET | `/repair-jobs/{job_id}` | 受影响文件/piece、隔离计划和结果 |
| POST | `/repair-jobs/{job_id}/actions` | `approve`、`cancel`、`retry` |

### 5.6 设置、通知、日志与升级

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/PATCH | `/settings/{namespace}` | 匹配、调度、安全和保留策略 |
| GET/POST | `/notification-channels` | 通知渠道列表与创建 |
| POST | `/notification-channels/{id}/test` | 发送脱敏测试消息 |
| GET | `/logs` | 按级别、时间、任务、trace_id 查询结构化日志 |
| POST | `/diagnostics/export` | 生成有有效期的脱敏诊断包 |
| GET | `/events/stream` | 管理界面使用的 SSE 状态流 |
| GET | `/updates/check` | 查询可用版本及兼容信息 |
| POST | `/updates/apply` | 显式确认后启动升级 |
| GET | `/updates/{update_id}` | 升级、健康检查和回滚进度 |

## 6. 核心资源形状

### 6.1 任务摘要

```json
{
  "id": "UUID",
  "type": "PACKAGE_UNPACK",
  "status": "PREFLIGHT",
  "source": {
    "downloader_id": "UUID",
    "torrent_hash": "redacted-fingerprint",
    "display_name": "Synthetic.Collection.2026"
  },
  "progress": {"completed_units": 2, "total_units": 10},
  "requires_attention": true,
  "trace_id": "UUID",
  "version": 7,
  "created_at": "2026-09-05T08:30:00Z",
  "updated_at": "2026-09-05T08:35:00Z"
}
```

### 6.2 预演快照

预演必须包含：候选身份、评分证据、硬约束、验证等级、逐文件映射、目标路径、计划动作、预计下载字节、空间要求、冲突、风险和快照版本。批准请求必须引用预演 ID；源状态或候选元数据变化后旧预演失效。

## 7. HTTP 状态语义

- 200：同步读取或幂等重放已有结果。
- 201：资源已创建。
- 202：异步动作已接受。
- 204：成功且无响应体。
- 400：请求语法或不支持的组合。
- 401/403：未认证/无范围权限。
- 404：资源不存在或调用者不可见。
- 409：状态、幂等或资源冲突。
- 412：`If-Match` 版本不一致。
- 422：字段有效但不满足领域约束。
- 429：调用方或外部适配器限流。
- 503：系统未就绪或关键依赖暂不可用，并返回 `Retry-After`。

## 8. 下载完成 Webhook

端点：`POST /api/v1/integrations/download-completed`。

请求头：

- `X-PackBreaker-Key-Id`：定位验签密钥，不是秘密。
- `X-PackBreaker-Timestamp`：Unix 秒，默认允许前后 300 秒。
- `X-PackBreaker-Nonce`：一次性随机值。
- `Idempotency-Key`：调用事件唯一键。
- `X-PackBreaker-Signature`：`sha256=<hex(HMAC(secret, canonical_request))>`。

canonical request 固定为：方法、路径、时间戳、nonce、幂等键和原始 body SHA-256，以换行连接。服务端先限制 body 大小，再以常量时间比较签名，最后落库 nonce 与幂等记录。

请求体只接收下载器 ID、torrent hash、事件时间和可选分类/标签提示；服务端必须回查下载器真实任务状态，不信任调用方提供的路径、完成率和文件列表。

## 9. API 兼容与测试

- OpenAPI 文件纳入版本控制；后端 CI 检测未声明的不兼容变化。
- 前端类型从当前 OpenAPI 生成，禁止手写重复且可能漂移的资源类型。
- 每个写端点覆盖认证、CSRF/Token 范围、幂等重放、并发冲突、无效状态和脱敏错误测试。
- 适配器原始错误必须转换为稳定领域错误码，不能直接成为公共 API 契约。
