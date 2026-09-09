# 适配器开发规范

## 1. 目的

适配器负责隔离 PT 站点、下载器和通知渠道的协议差异。核心流程只依赖规范化 DTO、能力集和稳定异常，不能读取第三方原始 JSON、HTML、RPC 字段或 SDK 对象。

每个适配器由以下部分组成：

- 配置 schema 与凭证引用。
- 能力声明。
- 协议客户端与响应解析器。
- 规范化 DTO 转换。
- 错误分类、重试提示和脱敏规则。
- 契约测试与合成响应夹具。

## 2. 通用规则

- 构造适配器时注入已解密的短生命周期凭证视图；不得把凭证写入实例 repr、异常或缓存键。
- 所有方法接受 deadline/cancellation context，设置连接和读取超时。
- 适配器不自行无限重试；只返回 `retryable` 和建议等待时间，由应用层统一调度。
- 原始响应仅用于内存解析。调试留存必须显式开启、先脱敏、限制大小和保留时间。
- 能力不支持时抛出 `CapabilityNotSupported`，不能静默模拟成功。
- 外部资源 ID 与显示名称分离；业务唯一性只使用稳定 ID。

## 3. 站点适配器

### 3.1 契约

```python
class SiteAdapter(Protocol):
    async def capabilities(self) -> SiteCapabilities: ...
    async def test_connection(self) -> ConnectionTestResult: ...
    async def search(self, query: SearchQuery) -> SearchPage: ...
    async def fetch_details(self, torrent_id: str) -> TorrentDetails: ...
    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload: ...
```

`TorrentPayload` 包含受限大小的 bencoded bytes、来源站点、远程 ID、获取时间和内容摘要。它不得向 API 或日志暴露下载 URL、passkey 或响应头。

### 3.2 规范化搜索模型

`SearchQuery` 支持：关键词、媒体类型、季/集、外部 ID、分页和排序提示。适配器只能发送站点实际支持的字段，并在 capabilities 中声明：

- 是否支持 IMDb/豆瓣 ID 搜索。
- 是否支持精确短语、分类、分页和详情文件列表。
- 是否需要先取下载令牌。
- 建议最小请求间隔和已知限流语义。

领域层统一使用 `CandidateMeta` 承接搜索结果：至少包含站点稳定 ID、远程 torrent ID、标题，并可携带站点声明的大小、发布时间、分类、做种/下载人数、外部 ID 与可选文件摘要。站点未提供的字段为 `null`，不得猜测。`CandidateMeta` 中的文件摘要仍只是搜索/详情页声明；只有下载 `.torrent` 并通过安全 parser 后，文件路径、长度、piece 等字段才成为可用于内容验证的协议事实。

`SearchQuery.query_text` 保持未 URL 编码；具体 adapter 根据站点契约编码参数。核心层最多生成少量、确定性的逐步放宽查询，不允许 adapter 自行把评分结果解释成自动执行许可。

### 3.3 首批实现

- `MTeamAdapter`：使用官方 API；API Key 放在 `x-api-key`；下载令牌不落库，按最短必要时间缓存。
- `NexusPhpAdapter`：封装通用搜索/详情/下载流程；站点变体通过 profile 配置选择解析器，不用大量 if/else 混入核心类。
- `HDTimeAdapter`：基于 NexusPHP profile，实现站点特有字段映射与契约测试。
- `HHClubAdapter`：确认引擎和规则后选择 profile 或独立适配器；确认前不得按猜测上线。

HTML 页面解析优先使用 DOM 解析器和稳定选择器。登录失效、验证码、Cloudflare、页面结构变化必须返回明确错误并熔断自动化，禁止绕过站点保护。

## 4. 下载器适配器

### 4.1 契约

```python
class DownloaderAdapter(Protocol):
    async def capabilities(self) -> DownloaderCapabilities: ...
    async def test_connection(self) -> ConnectionTestResult: ...
    async def list_completed(self, rule: MonitorRule) -> list[SourceTorrent]: ...
    async def get_torrent(self, torrent_hash: str) -> DownloaderTorrent: ...
    async def get_files(self, torrent_hash: str) -> list[DownloaderFile]: ...
    async def add_torrent(self, request: AddTorrentRequest) -> AddedTorrent: ...
    async def start_verify(self, torrent_hash: str) -> None: ...
    async def get_verify_status(self, torrent_hash: str) -> VerifyStatus: ...
    async def pause(self, torrent_hash: str) -> None: ...
    async def resume(self, torrent_hash: str) -> None: ...
    async def remove_torrent(self, torrent_hash: str) -> None: ...
```

`remove_torrent()` 的契约固定为只移除下载器任务、不删除数据；实现调用第三方 RPC 时必须显式传递“不删除数据”。首版不定义任何删除下载数据的适配器方法。即使未来开放，也必须是独立的高风险能力，不能作为普通回滚的一部分。

### 4.1.1 M1 已实现边界

M1 只实现下载器配置所需的只读探测切片：qBittorrent 可使用用户名/密码登录或 API Key，并读取应用/WebAPI 版本；Transmission 使用 RPC `session-get` 完成 session-id 握手并读取客户端/RPC 版本。生产适配器当前只暴露 `test_connection()`，不实现 `list_completed`、`get_torrent`、`add_torrent`、校验、暂停、恢复或移除等任务方法。完整契约仍作为 M3/M4 的目标接口，不得因配置阶段提前引入下载器写副作用。

连接探测在数据库事务之外执行，结果只在配置 version 未变化时写回；错误只返回稳定分类和脱敏描述，不持久化第三方响应正文、请求头或凭证。

### 4.2 添加任务约束

`AddTorrentRequest` 包含 torrent payload 引用、目标保存路径、分类/标签、paused、skip_checking 和执行计划 ID。

- 默认 `paused=True`、`skip_checking=False`。
- 应用层只有在执行计划仍有效且验证等级为 `FULL_VERIFIED` 时才能设置 qB `skip_checking=True`。
- Transmission 不声明 skip checking 能力，添加后必须完整校验。
- 适配器收到不支持或不安全的组合时再次拒绝，形成双层保护。
- 添加完成后返回客户端实际 torrent hash；应用层核对期望 hash 并写入操作日志。

### 4.3 路径映射

下载器返回的是下载器视角路径。映射规则由 `(remote_prefix, container_prefix)` 组成，采用最长前缀匹配，并满足：

- 两侧均规范化为绝对路径。
- 只允许映射到配置的 source roots。
- 解析符号链接后仍必须位于允许根目录。
- 可先保存未启用配置；启用自动化前必须使用已存在测试文件覆盖并通过每一条映射的双向诊断。
- 多条规则同长度且同时命中时视为歧义并阻断。

## 5. 通知适配器

```python
class NotificationProvider(Protocol):
    async def test_connection(self) -> NotificationTestResult: ...
    async def send(self, message: NotificationMessage) -> DeliveryResult: ...
```

`NotificationMessage` 是已脱敏的标题、正文、严重级别、事件键和可选站内链接。Provider 不能访问任务完整日志或 secret store。

- 首版实现 Server酱和 Telegram。
- 相同事件键在聚合窗口内只发送一次，重复次数在下一条摘要中体现。
- 通知失败不得改变辅种任务结果；独立重试并设置上限。
- Telegram chat ID、bot token 和 Server酱 SendKey 均按秘密处理。

## 6. 能力模型

能力集使用明确布尔值和版本信息，禁止根据 adapter 类型在核心代码硬编码：

```text
SiteCapabilities
  search_by_external_id
  search_categories
  details_include_files
  requires_download_token
  pagination

DownloaderCapabilities
  skip_checking
  labels
  categories
  force_recheck
  verify_progress
  rename_files
  api_version
```

能力在连接测试后持久化，并记录探测时间。版本变化或超过 TTL 后重新探测；关键能力变化时暂停依赖该能力的自动任务。

## 7. 错误分类

| 异常 | 可重试 | 处理 |
| --- | --- | --- |
| `AuthenticationFailed` | 否 | 禁用自动化并通知，等待更新凭证 |
| `AuthorizationDenied` | 否 | 记录缺失能力，不重复请求 |
| `RateLimited` | 是 | 使用服务端 Retry-After 或指数退避 |
| `TemporaryUnavailable` | 是 | 计入熔断器，有限重试 |
| `InvalidResponse` | 条件性 | 保存脱敏摘要，达到阈值后熔断 |
| `ResourceNotFound` | 否 | 候选失效或下载器任务已移除，触发对账 |
| `Conflict` | 条件性 | 查询真实状态后决定 NOOP 或人工处理 |
| `CapabilityNotSupported` | 否 | 阻断相关动作并展示能力限制 |

异常必须包含 adapter 名称、操作、稳定错误码、retryable 和脱敏上下文；不得包含请求头、Cookie、完整 URL 查询串或响应正文。

## 8. 限流、重试与熔断

- 每站点独立 token bucket；默认并发 1，具体速率由站点配置和规则决定。
- 只重试幂等读取；下载令牌、添加种子等操作在重试前必须查询真实状态或使用稳定幂等依据。
- 指数退避带随机抖动，尊重 Retry-After；最大尝试次数和总 deadline 均有限。
- 连续鉴权失败立即打开熔断；临时错误达到阈值后打开，半开阶段只允许单个探测请求。
- 搜索缓存键只包含规范化查询和非敏感站点 ID；凭证变化、站点禁用和适配器版本变化时失效。

## 9. 契约测试

每个适配器必须通过共享测试套件：

- capabilities、连接成功/失败和超时。
- 规范化 DTO 的必填字段、分页和缺失字段。
- 限流、临时错误、鉴权失效和不支持能力。
- 日志、异常、repr 和快照中无秘密。
- 同一输入返回稳定身份与内容摘要。
- 下载器添加动作的暂停、安全校验和重复调用语义。

测试只使用合成响应、录制后彻底脱敏的结构夹具或本地模拟服务。禁止 CI 访问真实 PT 站点和真实下载器。
