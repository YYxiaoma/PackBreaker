# 数据拆包 v2 架构设计

## 1. 目的

本文把已确认原型中的“数据拆包”交互转换为可实现的后端架构与状态机。核心目标是统一手动 / 监控两种触发方式，避免继续维护两套扫描、匹配、审核和执行逻辑。

## 2. 总体链路

统一执行链：

Task Definition
→ 手动触发 / Cron 监控触发
→ Unpack Execution
→ 分页发现媒体
→ 持久化 Execution Items
→ 分批 Match Coordinator
→ 自动匹配 / 人工审核 / 匹配异常
→ 获取候选 .torrent
→ v1 / v2 / hybrid 内容级只读验证
→ 安全 Execution Plan
→ Operation Journal + 下载器 / 文件系统副作用
→ 客户端 Verify / Reconcile / Finish

任务定义只保存长期配置；每次执行必须冻结完整配置快照，防止执行过程中修改任务定义导致当前 run 语义漂移。

## 3. 核心组件

### 3.1 UnpackDefinitionService

负责：

- 创建、编辑、删除数据拆包任务定义；
- 校验手动 / 监控触发类型；
- 规范化来源、文件过滤、站点、输出和重试配置；
- 为 execution 生成冻结配置快照。

不负责站点搜索和文件副作用。

### 3.2 MediaDiscoveryService

负责从来源产生待处理媒体对象。

目录来源：

- 从授权根开始；
- 使用 cursor 分页扫描；
- 应用后缀、大小、名称等过滤；
- 为每个对象建立 source snapshot；
- 不修改源文件。

下载器来源：

- 从 qBittorrent / Transmission 读取任务对象；
- 根据现有路径映射得到容器可见媒体；
- 最终仍输出与目录来源一致的媒体对象模型；
- 后续匹配链不区分来源种类。

手动目录任务还需要提供“保存前影视文件扫描”能力：

- 仅在用户保存时选择“否，选择影片”后触发；
- 扫描结果分页返回，不直接创建 execution item；
- 支持关键字、后缀、分辨率、已选状态等前端过滤；
- 用户最终保存的只是选定媒体的稳定 source object identity，而不是一次 execution。

执行范围分两种：

- `ALL_MATCHING_MEDIA`：点击“执行”时按当时目录内容和任务过滤规则重新发现全部影视文件；
- `SELECTED_MEDIA`：点击“执行”时只解析保存时选定的 source object identity，并重新做路径授权、存在性和 snapshot 校验。

### 3.3 MatchCoordinator

负责领取 MATCH_PENDING item 并分批处理。

职责：

- 控制 batch size；
- 限制单站点并发；
- 选择搜索依据；
- 调用站点只读 search adapter；
- 去重候选；
- 执行相关性门；
- 调用 CandidateScorer；
- 保存候选与评分证据；
- 根据任务自动匹配阈值决定 MATCHED_AUTO 或 REVIEW_REQUIRED。

### 3.4 CandidateScorer

CandidateScorer 必须是纯领域逻辑，不能直接访问网络。

输入：

- SourceMediaIdentity；
- SiteCandidate。

输出：

- score；
- evidence；
- hard conflicts；
- is_exact_match。

is_exact_match 必须由显式规则计算，不允许把四舍五入后的 100.0 当成完全匹配。

自动匹配判定与 is_exact_match 分开：score 达到任务阈值且不存在 hard conflict 时可以 MATCHED_AUTO；is_exact_match 作为更强元数据证据继续保留，但无论是否 exact，都必须经过 torrent 内容验证。

### 3.5 ReviewService

负责：

- 读取 item 候选；
- 计算默认候选；
- 提交人工选择；
- 标记无匹配；
- expected revision 并发保护。

“默认选中最高匹配度”只体现在读取响应和前端初始选择，不应在打开审核弹窗时产生数据库写入。

自动匹配成功的 item 同样可以进入 ReviewService：在尚未发生外部副作用前允许用户改选候选或标记无匹配；一旦进入文件 / 下载器写操作阶段，只允许查看审核证据，若要改变候选必须先安全取消并回滚。

### 3.6 TorrentContentVerifier

负责在候选进入辅种执行前证明“本地文件字节与候选 torrent 内容一致”。

处理流程：

- 通过站点 adapter 获取候选 `.torrent`；
- 解析 v1 / v2 / hybrid metainfo；
- 建立 torrent file → 本地文件映射；
- v1 使用 piece SHA-1 验证 torrent 逻辑字节流；
- v2 使用 SHA-256 Merkle pieces root / piece layers 验证每个文件；
- hybrid 必须同时通过 v1 与 v2；
- 验证前后复核 source snapshot，防止 TOCTOU。

只有 `FULL_VERIFIED` 才能自动进入辅种执行。`CLIENT_CHECK_REQUIRED` 不等同于匹配成功；在本版本“只为完全相同影片辅种”的目标下，默认不得把它当成自动执行条件。

#### 3.6.1 主媒体与辅助文件分类

TorrentContentVerifier 在建立文件映射时必须区分文件角色：

- `PRIMARY_MEDIA`：当前需要辅种的主影片，必须映射到本地源文件；
- `AUXILIARY_FETCHABLE`：允许本地缺失的小型辅助文件，例如 `.nfo`、海报图片、字幕和文本 sidecar；
- `REQUIRED_MEDIA`：其他视频 / 音频媒体，默认不能因为缺失而忽略；
- `PROTOCOL_PADDING` / `ZERO_LENGTH`：按 BitTorrent 协议规则处理。

辅助文件白名单必须显式维护，不能把所有小文件都当成可忽略内容。另一个正片、额外集数、花絮视频、sample 视频等默认属于 REQUIRED_MEDIA。

#### 3.6.2 辅助文件安全补齐

当主影片映射可信但 AUXILIARY_FETCHABLE 缺失时，item 进入“辅助文件待补齐”而不是“内容不一致”。执行步骤：

1. 创建 PackBreaker 管理的隔离 staging 目录；
2. 以暂停状态把候选 torrent 加入下载器；
3. 使用下载器按文件选择能力，只把缺失辅助文件设为 wanted；
4. 主影片源路径不 hardlink 到 staging，避免客户端下载 / 修复动作触碰源影片 inode；
5. 对 v1 多文件 torrent，允许客户端下载跨文件边界 piece，但所有写入都发生在 staging；
6. 辅助文件就绪后，用“本地主媒体 + staging 辅助文件”重新运行完整 piece / Merkle 校验；
7. 达到 FULL_VERIFIED 后，才允许生成最终辅种 Execution Plan；
8. staging 中由 PackBreaker 获取的辅助文件可以移动到最终目标目录；
9. staging 临时对象按 operation journal 清理。

这种方式同时解决 v1 piece 可能跨越主影片与 `.nfo` / 海报边界的问题：只有拿到辅助文件实际字节以后，才能完整计算跨边界 piece hash。

下载器 adapter 需要新增 `supports_selective_files` capability，以及“设置 wanted / unwanted 文件”的统一接口。若当前下载器不支持该能力，则带缺失辅助文件的候选不能自动补齐，必须显示“当前下载器不支持辅助文件补齐”。

### 3.7 UnpackExecutionService

负责把已经确认的候选接入既有安全执行链。

消费：

- source snapshot；
- approved candidate；
- torrent content verification evidence；
- target downloader；
- output policy。

继续复用：

- Execution Plan；
- 风险检查；
- operation journal；
- 下载器动作幂等；
- 文件 ownership / inode / snapshot；
- verify / reconcile / rollback。

## 4. 分页媒体发现

### 4.1 为什么必须分页

大目录可能包含数万对象，一次加载会导致：

- 内存峰值；
- HTTP 超时；
- execution 无法中断恢复；
- 页面长期没有进度；
- 任意一次失败都需要从头扫描。

因此 discovery 必须是后台分页任务，不依赖前端保持连接。

### 4.2 Cursor 设计

cursor 是服务端不透明值，至少绑定：

- source kind；
- 排序键；
- 最后处理的 source identity；
- execution 配置快照版本。

客户端不能自行拼接路径作为 cursor。

### 4.3 稳定排序与 watermark

目录建议按规范化绝对路径稳定排序；下载器来源使用 downloader object identity + file path 排序。

同一次 execution 中：

- 已产生的 item 使用创建时 snapshot；
- execution 建立 discovery watermark；
- 新文件是否进入当前 execution 按 watermark 规则决定；
- 文件插入不能造成旧 item 重复或遗漏。

## 5. SourceMediaIdentity

建议领域对象包含：

- source_object_key；
- canonical_path；
- filename；
- size_bytes；
- mtime_ns；
- device；
- inode；
- title；
- year；
- imdb_id；
- douban_id；
- resolution；
- source_medium；
- release_group。

媒体解析字段可以为空，但 source object identity 与文件安全快照必须完整。

## 6. 搜索策略

单个媒体对每个目标站点的搜索优先级：

1. IMDb ID；
2. 豆瓣 ID；
3. 规范化影片名 + 年份；
4. 必要时退化为规范化影片名。

每次 search attempt 记录：

- site_id；
- query_kind；
- query_fingerprint；
- started_at / finished_at；
- result_count；
- error_code；
- retry_count。

不得记录 Cookie、passkey、token、完整私有 URL 或受保护的第三方原始响应。

### 6.1 PT 站点能力适配

搜索与内容验证必须以站点 adapter 的 capability 为准，不能假设所有 PT 站都支持同一种查询方式。

站点能力至少包括：

- 是否支持 IMDb ID；
- 是否支持豆瓣 ID；
- 是否支持分页；
- 是否需要下载 token；
- 最小请求间隔；
- 单次任务允许下载多少个候选 torrent 做内容验证。

若站点不支持 IMDb / 豆瓣 ID，则自动退化为标题 + 年份搜索；若站点不能安全获取 `.torrent`，该站点候选可以用于展示，但不能进入自动辅种执行。

M-Team、NexusPHP 等当前适配器已经具备外部 ID 搜索和 torrent 获取能力；实现时继续通过 adapter 统一接口调用，不在 MatchCoordinator 中写站点特例。

## 7. 候选去重、相关性与评分

同站点候选优先按稳定 torrent / post ID 去重；没有稳定 ID 时使用受控 candidate fingerprint。

处理顺序固定为：

1. hard conflict；
2. 相关性门；
3. 候选评分；
4. exact-match 判断；
5. 自动匹配阈值判断。

评分证据可以包括：

- IMDb ID 一致；
- 豆瓣 ID 一致；
- 标题 / 别名一致；
- 年份一致；
- 分辨率一致；
- source medium 一致；
- release group 一致；
- 文件大小或总大小接近。

前端必须能够解释匹配分数，不只显示裸分数。

## 8. 自动匹配阈值与内容验证

任务定义提供 `auto_match_threshold`，前端显示“自动匹配阈值（%）”，默认 100.0%。

候选 score 达到阈值且不存在 hard conflict 时可以自动选中；低于阈值时进入人工审核。`is_exact_match` 继续作为强元数据证据，但不再是是否自动选择的唯一条件。

建议 exact-match 至少要求：

- 存在强身份信息时外部 ID 不冲突；
- 标题规范化完全一致或命中明确别名；
- 年份无冲突；
- 分辨率一致；
- source medium 一致；
- release group 在可解析时一致；
- 大小在定义的精确容差内；
- 不存在任何 hard conflict。

自动选中后：

- item → MATCHED_AUTO；
- 保存选中候选；
- 保存评分证据；
- 进入 TorrentContentVerifier。

自动匹配成功不代表可以绕过 torrent、文件系统或下载器安全验证。

### 8.1 为什么不能把本地影片 hash 与 info hash 直接比较

BitTorrent v1 info hash 是 torrent `info` 字典的 SHA-1，v2 info hash 是对应元数据结构的 SHA-256；它们标识 torrent 元数据，不是单个影片文件的整体内容 hash。

真正证明本地影片是否与 torrent 一致，需要使用 torrent 自带的内容校验数据：

- v1：`pieces` 中逐 piece SHA-1；
- v2：每个文件的 `pieces root` 与 piece layers；
- hybrid：两种验证都通过。

多文件 v1 torrent 的 piece 可能跨越文件边界，因此也不能简单对每个本地文件做一个 SHA-1 后与 torrent 某字段直接比较。

### 8.2 内容验证结果

结果分三类：

- `FULL_VERIFIED`：允许进入辅种执行；
- `CLIENT_CHECK_REQUIRED`：现有证据不足以由 PackBreaker 本地证明完整一致，本版本默认不自动辅种；
- `BLOCKED` / 内容不一致：禁止辅种。

候选内容不一致时，优先遵守站点 `max_verification_candidates` 和请求间隔限制：若仍有验证预算，可尝试下一条高分候选；否则进入人工审核并展示内容不一致证据。

## 9. 人工审核状态

最佳候选低于自动匹配阈值，或自动候选内容验证失败后需要人工选择时：

- 保存完整候选列表；
- 按 score 降序；
- item → REVIEW_REQUIRED；
- execution 统计待人工审核数量；
- UI 默认选择最高 score 候选；
- 用户显式确认后才进入 MATCHED_MANUAL。

人工确认只改变候选选择，随后仍必须进入 TorrentContentVerifier；人工批准不能覆盖内容校验失败。

允许操作：

- 选择任一候选并确认；
- 标记无匹配；
- 稍后处理。

审核后候选不能被下一次后台搜索静默替换。重新搜索必须显式产生新的 candidate generation。

## 10. 匹配异常与重试

状态区分：

- MATCH_TIMEOUT；
- MATCH_ERROR；
- NO_MATCH。

NO_MATCH 不是系统异常；MATCH_TIMEOUT / MATCH_ERROR 才进入异常重试链。

自动重试由任务定义中的：

- auto_retry_enabled；
- max_auto_retries；

控制。

重试只处理指定 item：

- 不重新扫描整个来源；
- candidate generation + 1；
- 保留旧候选 generation 作为审计历史；
- retry_count 持久化；
- 达上限后等待人工重试。

## 11. 任务定义与 Execution 状态

### 11.1 任务定义状态

新建任务保存后统一进入：

- `PENDING_EXECUTION`：前端显示“待执行”；
- `ENABLED`：监控任务点击执行后进入启用状态；
- `PAUSED`：暂停；
- `ERROR`：定义或监控触发异常。

保存任务本身不得创建 execution。任务列表的“执行”动作才是运行边界：

- 手动任务：创建新的 execution；
- 监控任务：将定义从 PENDING_EXECUTION 切到 ENABLED，之后由 Cron / 监控来源创建 execution。

### 11.2 Execution 聚合状态

建议 execution 状态：

- DISCOVERING；
- MATCHING；
- REVIEW_REQUIRED；
- CONTENT_VERIFYING；
- EXECUTING；
- CLIENT_VERIFYING；
- COMPLETED；
- COMPLETED_WITH_ERRORS；
- PAUSED；
- FAILED；
- CANCELLED。

item 状态建议：

- DISCOVERED；
- MATCH_PENDING；
- MATCHING；
- MATCHED_AUTO；
- REVIEW_REQUIRED；
- MATCHED_MANUAL；
- NO_MATCH；
- MATCH_TIMEOUT；
- MATCH_ERROR；
- TORRENT_FETCHING；
- AUXILIARY_FETCHING；
- CONTENT_VERIFYING；
- CONTENT_VERIFIED；
- CONTENT_MISMATCH；
- PLAN_PENDING；
- EXECUTING；
- CLIENT_VERIFYING；
- COMPLETED；
- EXECUTION_ERROR；
- CANCELLED。

聚合状态由 item 集合计算，不允许任意单 item 直接覆盖 execution。

## 12. 分项继续策略

一个 execution 中若：

- A 已达到自动匹配阈值并完成内容验证；
- B 等待人工审核；
- C 匹配超时；

则：

- A 可以继续生成安全执行计划并完成；
- B 保持 REVIEW_REQUIRED；
- C 显示重试；
- execution 继续展示聚合进度和异常计数。

因此一个待审核对象不会阻断已经明确可执行的其他对象。

## 12.1 前端状态中文化

内部枚举可以继续使用英文，但前端所有可见状态、阶段、错误和操作都必须映射为中文。例如：

- DISCOVERING → 正在发现影片；
- MATCHING → 正在匹配；
- MATCHED_AUTO → 自动匹配成功；
- REVIEW_REQUIRED → 待人工审核；
- TORRENT_FETCHING → 正在获取种子；
- AUXILIARY_FETCHING → 正在补齐辅助文件；
- CONTENT_VERIFYING → 正在校验影片内容；
- CONTENT_VERIFIED → 内容校验通过；
- CONTENT_MISMATCH → 内容不一致；
- EXECUTING → 正在辅种；
- CLIENT_VERIFYING → 下载器校验中；
- COMPLETED → 已完成；
- MATCH_TIMEOUT → 匹配超时；
- MATCH_ERROR → 匹配错误。

英文枚举仅保留在 API、日志技术字段和必要的排障 Tooltip 中。

## 13. 并发与租约

建议：

- 一个 execution 同时只有一个 discovery lease；
- match worker 可并发领取多个 item；
- 同一 item 使用 version / lease token 防重复领取；
- 同一站点设置并发上限；
- matcher 与人工审核通过 revision CAS 防止覆盖；
- 外部副作用继续由 operation journal 仲裁。

## 14. 暂停、取消与恢复

暂停：

- 不领取新 discovery page；
- 不领取新 match batch；
- 已发出的只读搜索允许完成；
- 已进入外部写操作的 item 必须按 journal 收敛。

取消：

- 未产生副作用的 item 可直接 CANCELLED；
- 已产生副作用的 item 按 operation journal 执行可证明的安全取消 / 回滚。

进程重启：

- discovery 从 cursor 恢复；
- matcher 从非终态 item 恢复；
- review 状态不丢失；
- external write 从 journal 恢复，禁止盲目重发。

## 15. 目录树服务

目录树只能展示管理员显式授权路径。

服务端输入：

- root key；
- parent node token；
- optional query。

返回：

- node id / token；
- display name；
- node type；
- has_children；
- child_count（可选）；
- estimated_size（可选）。

保存任务时根据 selection token 重新解析 canonical path 并再次执行 AuthorizedPathScope 校验。

禁止暴露：

- 未授权根；
- /config；
- 系统目录；
- socket；
- secret；
- 符号链接逃逸目标。

## 16. 后缀名过滤

后缀白名单内部存为规范化数组，例如：

- .mkv
- .mp4
- .ts

规则：

- trim；
- lowercase；
- 自动补前导点；
- 去重；
- 拒绝路径分隔符、NUL 和超长值；
- 空数组代表不限制；
- downloader 来源最终映射出的实际文件同样应用后缀过滤。

## 17. 与数据去重的领域边界

“数据拆包”和“数据去重”只共享任务中心一级入口。

数据去重继续使用自己的：

- job；
- duplicate pair；
- link strategy；
- atomic replacement；
- cross-device handling。

禁止为了 UI 两卡化而把去重与拆包硬塞进同一 execution item 模型。
