# 研发路线图

## 1. 推进原则

里程碑按安全依赖顺序推进。后续阶段可以提前做无依赖的 UI 原型或适配器研究，但不能在前置安全门未完成时接入真实下载器执行写操作。

```mermaid
flowchart LR
    M0[M0 基线与语料] --> M1[M1 安全骨架]
    M1 --> M2[M2 解析、匹配与预演]
    M2 --> M3[M3 qB 安全主链路]
    M3 --> M4[M4 TR、站点可靠性与修复]
    M4 --> M5[M5 历史与剧集辅种]
    M5 --> M6[M6 发布与运维闭环]
```

每个阶段满足退出条件后才能标记完成。阶段内功能完成但测试、文档或恢复演练未完成，仍视为进行中。

## 2. M0：基线与语料

### 交付项

- 冻结需求基线 v0.3 和本套研发文档。
- 收集蜂巢 3.3T 大包的脱敏目录树、torrent 结构摘要和日志。
- 对 7 个失败样例建立编号、触发条件、期望结果和归因。
- 确认 HHClub 引擎、鉴权、自动化限制和搜索/取种方式。
- 确认 qB/TR 版本、NAS 文件系统、容器路径和目标目录布局。
- 建立合成 torrent/媒体生成器规格，不提交真实数据。

### 退出条件

- 每类真实结构至少有一个可公开提交的合成等价案例。
- 下载器与文件系统版本矩阵明确。
- 匹配阈值仍可待标定，但默认人工确认策略已锁定。

## 3. M1：安全骨架

### 后端与数据

- 创建 Python/uv 项目、分层包结构、FastAPI、配置加载和结构化日志。
- 建立 SQLAlchemy、SQLite WAL、Alembic、实例锁和基础 repository。
- 实现管理员初始化、会话、CSRF、API Token、secret store 与统一脱敏。
- 实现任务/事件/操作日志最小模型和状态转换框架。

### 前端与部署

- 创建 Vue/TypeScript/pnpm 项目、路由、布局、认证和生成 API 客户端。
- 建立 Docker 多阶段构建、Compose、存活/就绪检查。
- 实现下载器 CRUD、连接测试和路径映射诊断页面。

### 测试与退出条件

- CI 完成格式、lint、类型、单元测试、前端构建和镜像构建。
- 凭证 canary 在日志、API、数据库普通字段中均不可见。
- qB/TR fake adapter 通过基础契约；路径穿越和映射歧义被阻断。
- 应用重启后配置、会话撤销状态和空任务队列保持一致。

当前实现进度：后端下载器 CRUD、加密凭证绑定、qB/TR fake/read-only probe、路径映射与临时 hardlink 诊断已落地；前端已通过 Axios + Pinia 接入管理员首次初始化/登录/退出、API Token 管理，以及下载器 CRUD、连接测试、多映射诊断、启停、CSRF 与 `If-Match`。OpenAPI schema snapshot 与生成 TypeScript 类型已纳入漂移检查；`uv.lock`、Docker 三阶段单镜像、Compose、FastAPI 同源前端静态资源、统一 check/test 脚本和 GitHub Actions 质量/浏览器/容器 smoke 门禁也已落地。凭证 canary、结构化日志脱敏、仓库敏感信息/大文件扫描以及配置/会话撤销/空队列重启一致性均已有自动化证据，M1 代码门的逐项清单见 `docs/m1-exit-checklist.md`。正式里程碑切换仍受 M0 中真实语料、失败样例归因、HHClub 信息和真实下载器/NAS 版本矩阵约束；下载器任务写链路继续严格保留到 M3/M4。

## 4. M2：解析、匹配与预演

### 交付项

- 实现安全 bencode 与 v1/v2/hybrid TorrentMeta 解析。
- 实现大包单元识别、token 规范化、搜索查询和候选排序。
- 实现 M-Team 与 HDTime 站点适配器及共享契约测试。
- 实现唯一文件映射、流式 piece 验证、验证缓存和三种验证等级。
- 实现预演快照、逐文件证据、人工选择与映射编辑页面。

当前实现进度：协议安全底座已包含受限 bencode 解码器、原始 `info` 字节区间保留、v1/v2/hybrid `TorrentMeta` 统一模型、Info-hash/metainfo digest、路径安全、重复字段/整数/资源上限和 hybrid 一致性阻断；v1 piece 验证支持跨文件逻辑流、受限分块读取、虚拟 padding、MISSING/AMBIGUOUS 判定与源快照复查。v2 已按 BEP 52 校验 piece layer 到 pieces root，并支持 16 KiB 叶块的流式 Merkle 验证；hybrid 必须同时通过 v1/v2 且两轮映射快照一致。唯一文件映射已实现完整相对路径+长度、唯一 basename+长度、媒体 token+扩展名+长度三级确定性策略，源清单只读扫描不跟随符号链接；验证缓存键已绑定 metainfo digest、映射状态、device/inode/size/mtime/file type、算法版本和读取策略，命中前仍复查当前快照。媒体 token 规范化已区分年份、季集/范围集/EP/绝对集/Specials、分辨率、片源、编码、HDR、音轨和外部 ID；候选初始 100 分模型已保留维度证据、硬冲突、算法/配置版本且明确禁止由分数授予自动执行。文件型大包现在可按主媒体文件稳定识别 MOVIE/EPISODE task unit，忽略附件和未支持的光盘片段；站点无关 `SearchQuery` 支持关键词、媒体类型、季集、外部 ID、分页/排序并生成最多三条逐步放宽查询，`CandidateMeta/SearchPage/SiteSearchCapabilities` 已固定搜索结果契约且严格区分站点声明与安全解析后的 torrent 事实。正式 `SiteAdapter` 只读端口与共享 fake contract 已落地，M-Team 已实现 API Key 内存鉴权、连接探测、搜索、详情和有界 torrent-fetch HTTP 边界，并阻断凭证转发与站外下载 URL；站点配置现已通过 Alembic/SQLite 持久化，凭证模型已泛化为 `API_KEY`/`COOKIE` 并全部进入 SecretStore，M-Team 与 HDTime 都可通过同一 CRUD/强 ETag/连接测试/启停安全门管理，配置或凭证类型变化会使旧 capability 失效。NexusPHP Web profile 与 `HDTimeAdapter` 已加入 Cookie 只读搜索/详情/torrent-fetch、同源凭证保护、HTML/torrent 资源上限和共享 contract 合成验证。`AnalysisService` 已把启用站点接入只读 M2 编排：构建逐步放宽查询、多站搜索并隔离站点失败、先评分再仅对前 N 个非硬冲突候选拉详情/种子、安全解析、自动映射和 v1/v2/hybrid 验证；声明最小请求间隔的站点单轮只执行一条查询，避免同一调用内突发请求。任务级手动 Analyze 现已严格从 `PENDING/RETRY/PAUSED` 进入 `ANALYZING → SEARCHING → MATCHING → VERIFYING → PREFLIGHT`，每次状态推进使用短事务记录事件，外部网络、文件扫描和哈希不在事务中执行；失败仅在本次仍持有任务 version 时安全回到 `RETRY`。分析结束前会复核启用站点版本和完整源 inventory digest，最终 preflight snapshot 绑定进入 `PREFLIGHT` 后的 task version；`preflight_snapshot` 以只追加表保存稳定 digest、站点/候选/映射/验证证据。TaskUnit 与最新 preflight 的 Candidate 现已通过 SQLite 独立持久化；`GET /tasks/{id}/units`、`candidates`、`preflight` 与 `preflight/current` 已提供真实后端数据和 task/source/site 三类当前性判断。`GET /tasks`、`POST /tasks` 与 `GET /tasks/{id}` 已提供真实任务集合、幂等创建和详情；前端「任务中心」现直接读取 SQLite 任务，可登记真实任务并从任务行打开同一 task ID 的 Analyze 抽屉。「预演与确认」已切换为真实聚合并加入版本化人工审核编辑器；`task_review_revision` 以只追加完整状态保存批准候选、拒绝集合、人工歧义映射、操作者类型和 `requires_reverification`，`expected_version` 阻断并发覆盖。硬冲突候选不能被人工批准，人工映射只能选择当前 AMBIGUOUS 证据中的候选源文件；首个有效 revision 可通过唯一 `REVIEW_OPENED` bridge 将 `PREFLIGHT` 推进到 `AWAITING_CONFIRMATION`。人工映射/非完整验证候选现可显式 `reverify`：系统重新确认 current preflight、source inventory 与 review revision，重新获取同一 torrent 并核对 metainfo digest，应用人工映射后再次执行 v1/v2/hybrid 内容验证；`task_review_verification` 以只追加记录保存稳定 verification digest、映射和验证等级，重验证期间 review/preflight 变化时拒绝落库。M2 审核与重验证仍固定 `execution_allowed=false`，总览仍保留 `PB-*` 合成数据。批准执行、真实账号验收、真实语料阈值标定和 LINKING/下载器写链仍未完成。

### 退出条件

- 合成协议矩阵和恶意元数据测试全部通过。
- 真实语料能生成稳定、可解释的候选与预演，不执行下载器写操作。
- 同一输入重复分析结果稳定，源文件变化会使缓存和预演失效。
- 收集真实候选评分数据，形成阈值标定报告；未达到零误报证据前保持人工确认。

## 5. M3：qBittorrent 安全主链路

### 交付项

- 实现安全文件系统网关、目标校验、原子硬链接和 operation journal。
- 实现 qB 适配器：监控、文件查询、暂停添加、校验、状态确认和安全跳过校验。
- 完成任务协调器、并发限制、幂等键、取消、回滚和启动对账。
- 实现任务列表/详情/时间线、SSE 更新和 Server酱/Telegram 通知。

### 退出条件

- qB 使用合成与真实语料端到端完成，非 FULL_VERIFIED 从未跳过校验。
- 相同触发 10 次只生成一套资源和一个 qB 任务。
- LINKING、ADDING、CLIENT_VERIFYING 故障注入后正确收敛。
- 所有测试场景源文件 hash、inode、size、mtime 保持不变。

## 6. M4：Transmission、站点可靠性与修复

### 交付项

- 实现 Transmission 适配器与完整客户端校验流程。
- 确认并实现 HHClub 适配器。
- 实现站点限流、带抖动退避、缓存、熔断和聚合通知。
- 实现三种 99% 修复模式、跨文件 piece 分析、硬链接写入隔离和缺失小文件补齐。
- 实现清理/对账报告与人工修复清单。

### 退出条件

- qB/TR 各完成真实端到端任务。
- 7 个失败样例全部有稳定错误码、处理策略和回归测试，至少覆盖三类根因。
- 修复前目标已与源 inode 隔离；空间不足或无法暂停时自动阻断。
- 站点异常不会产生请求风暴，恢复后可控半开探测。

## 7. M5：历史影片与电视剧

### 交付项

- 实现历史扫描根目录、文件类型、排除规则、游标、暂停和断点恢复。
- 将扫描结果转换为普通 task/task_unit，复用匹配、安全门和执行链路。
- 实现电视剧季/集、范围集、Specials 和多版本识别。
- 提供批量预演、筛选、人工确认和失败重试。

### 退出条件

- 大目录重复扫描只处理新增或变化内容，不重复创建任务。
- 影片和剧集各完成扫描到辅种闭环。
- 暂停、重启和取消不丢游标，不影响已完成任务。

## 8. M6：发布与运维闭环

### 交付项

- 完成仪表盘、日志、诊断导出、依赖健康、资源清理和移动端体验。
- 完成一致性备份、恢复演练、Alembic 升级矩阵和升级中心失败回滚。
- 固化镜像 digest、SBOM、版本说明、部署指南和用户手册。
- 执行全部安全、协议、性能、崩溃恢复和真实环境验收。

### 退出条件

- 需求基线 v0.3 的 v1.0 验收项全部通过。
- 自动误辅种为 0，源文件无变化，凭证扫描无泄露。
- linux/amd64 镜像可从空配置安装、升级、回滚和恢复备份。
- 已知限制、支持版本、升级兼容矩阵和后续计划已发布。

## 9. 工作项拆分模板

每个 Issue 至少包含：

- 对应里程碑和需求/设计章节。
- 用户可观察的完成结果。
- 输入、输出、状态变化和错误码。
- 安全不变量与禁止行为。
- 单元、契约、集成或端到端测试场景。
- 数据迁移、API 和文档影响。
- 明确不在本工作项中的内容。

一个工作项应尽量在一个子系统内独立验收。涉及数据库、文件系统和下载器三方状态的工作，先拆出领域与 operation journal，再实现外部动作。

## 10. 风险登记

| 风险 | 影响 | 当前措施 | 决策点 |
| --- | --- | --- | --- |
| 真实大包命名差异大 | 识别和召回不足 | 合成语料占位，M0 获取真实结构 | M2 阈值标定 |
| 跨站 torrent 字节布局不同 | 误判或需要补齐 | 文件映射 + 完整 piece 验证 | M2 |
| 硬链接修复修改源数据 | 严重数据损坏 | 写入前复制并替换独立 inode | M3/M4 门禁 |
| NAS bind mount 导致 EXDEV | 无法硬链接 | 挂载共同父目录 + 路径诊断 | M1 |
| 站点页面/API 变化 | 自动化中断或账号风险 | 适配器、熔断、默认关闭自动化 | 每个站点发布前 |
| SQLite 与长任务争用 | UI 卡顿、任务失败 | 短事务、WAL、外部 I/O 不持锁 | M1/M3 压测 |
| docker.sock 权限过高 | 主机控制权暴露 | 默认不挂载、显式风险和审计 | M6 |
| 主密钥丢失 | 凭证不可恢复 | 独立备份说明、可重新录入 | M1/M6 演练 |

## 11. v1.0 后候选

- arm64/armv7 镜像。
- MoviePilot 插件。
- 更多 NexusPHP、Gazelle、UNIT3D 和官方 API 站点。
- 多个本地来源拼装同一候选。
- 站点级辅种配额、时间窗口和复用率优选。
- 从人工确认中生成可审查的规则建议。
- 飞书、企业微信和钉钉通知。

后续能力不得通过放宽 FULL_VERIFIED、源数据保护、路径安全和幂等要求实现。
