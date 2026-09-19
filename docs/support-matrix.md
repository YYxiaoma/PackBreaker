# PackBreaker v1.0 支持矩阵

本页定义当前明确支持或已经现场验证的运行边界。未列为“支持”的组合不等于一定不可用，但在取得对应协议、恢复和真实环境证据前不得作为正式兼容承诺。

## 1. 发布平台

| 项目 | 当前状态 | 说明 |
| --- | --- | --- |
| Linux `amd64` 容器 | 当前已发布目标 | v0.1.9 正式镜像仅包含 `linux/amd64`；正式发布身份必须使用完整 image digest。 |
| Linux 其他架构 | 未声明支持 | 尚无构建、恢复和性能验收矩阵。 |
| Windows / macOS 原生生产运行 | 未声明支持 | 可用于开发，但 v1.0 生产部署以 Linux 容器为边界。 |

**v1.0.0 研发中（尚未正式支持）**：源码已增加 `linux/arm64`（aarch64）双架构发布契约、原生 ARM64 CI 构建/测试任务、发布 manifest 平台验证及 Web 升级跨架构阻断。GitHub CI run `35433684405` 已通过原生 ARM64 后端测试、Docker 构建、启动/健康、隔离 hardlink/xattr、数据库备份验证/恢复，以及基于合成 ARM64 基线的真实 Docker updater 成功替换、故障回滚和一次性 helper E2E；独立 Candidate Docker E2E run `35433684406` 亦通过。run `35436525382` 已通过隔离真实 ARM64 下载器 API 测试，run `35440660113` 已通过两种下载器的已授权连续任务链，run `35447763395` 与 `35448894436` 已分别通过 qBittorrent/Transmission 单条合成任务从分析到 DONE 的真实 ARM64 客户端门禁。由于 v0.1.9 正式镜像仅含 AMD64，合成基线门禁不是 ARM64 正式跨版本升级或 Web UI 点击验收证据；真实 PT、浏览器人工审批和正式多平台镜像尚待验收。取得全部必要证据并发布 v1.0.0 后才能调整正式支持声明。ARMv7 不包含在本次目标中，详细退出条件见 [研发路线图](./development-roadmap.md)。

ARM64 业务测试范围：GitHub CI run `35434318531` 的原生 ARM64 job 已通过真实 ARM64 Docker 容器的隔离 v1/v2/hybrid 媒体读取、映射、FULL_VERIFIED 与损坏降级、合成文件 Hardlink 及只读源不变量检查；原生 ARM64 Runner 的 qBittorrent/Transmission **模拟适配器**、任务 journal 和 linking 回归也已通过。此证据不代表真实 qBittorrent/Transmission 服务在 ARM64 环境下完成端到端验收，亦不代表正式 ARM64 跨版本升级或发行镜像验收。

**隔离真实下载器 API 门禁已通过**：GitHub CI run `35436525382` 的原生 ARM64 job 已成功运行独立无外网 Docker qBittorrent 5.2.3 / Transmission 4.1.3 真实协议测试（仅合成种子和一次性配置），以及 repair inode 隔离、xattr 归属和 journal 崩溃恢复回归；qBittorrent 添加请求已通过提交 `a0b37db` 同时设置 `paused=true` 和 `stopped=true` 并验证任务真实停止状态。此项不是连接真实 PT/生产下载器的 E2E、完整任务生命周期或正式 ARM64 跨版本升级证据；不接触用户真实下载器或媒体。

**ARM64 已授权任务连续链路已通过**：GitHub CI run `35439015971` 已通过独立无外网 qBittorrent 容器上的真实应用层 ADDING、SEEDING、ADD/START journal 和重放机制，检查服务端 `DONE` 与真实客户端状态及文件 inode 一致性。run `35440660113` 已通过独立无外网 Transmission 容器中的 ADDING、CLIENT_VERIFYING、SEEDING、ADD/VERIFY/START journal 和源文件不变测试；该测试使用专用的 128 MiB 合成媒体以获得真实客户端校验过程的可观察证据。前置 ANALYZING/LINKING、Web UI 审批、正式跨版本升级和正式双架构发布仍需分别取得证据，不能把已授权任务切片扩大声明为全部正式支持。

Transmission 的较大合成样本曾暴露自动校验期间首次停止请求尚未收敛的竞态（CI run `35440957090`）；提交 `05431a9` 仅在确认 torrent 身份、归属与保存路径不变时限时只读等待停止状态，不重发外部写操作，超时仍要求对账。原生 ARM64 CI run `35442764543` 和独立 Candidate Docker E2E run `35442764564` 已通过修复后的门禁；该证据仅覆盖隔离已授权任务链，正式 ARM64 跨版本升级和完整前置任务生命周期仍未验收。

**已批准计划 → LINKING → 真实下载器门禁已通过**：GitHub CI run `35443372973` 在原生 ARM64 环境中，从 CI 合成的已批准 Execution Plan 交给正式 LINKING 协调器创建 Hardlink 与文件操作 journal，接续 qBittorrent / Transmission 的隔离真实客户端添加、校验、做种和幂等恢复；前置分析、审批动作和执行计划生成过程尚未在同一个真实客户端任务中贯通，不据此宣称全生命周期或正式 ARM64 支持。

**前置生命周期门禁已通过**：GitHub CI run `35446449569` 在原生 ARM64 Runner 上使用独立合成任务调用正式分析、审核、执行门、计划生成与 LINKING 服务，验证候选 FULL_VERIFIED、审批越权拒绝、无副作用计划及 Hardlink journal 幂等；站点、审批主体和目标下载器配置均为测试合成对象，并非真实 Web 人工操作或真实站点/下载器。与上述两种真实下载器后置切片尚未合并为同一任务的完整端到端验收。

**同一任务真实 qBittorrent 全链路门禁已通过**：GitHub CI run `35447763395` 在原生 ARM64 隔离 qBittorrent 测试中，以合成源文件/模拟站点和模拟管理员审批身份创建真实的任务、审核、执行计划及 Hardlink/ADD/START journal，并在同一任务中经真实客户端收敛到 DONE，含未授权失败关闭、重放与源文件安全检查；合成种子使用与前一项测试不同的信息哈希以防止误测已有任务，不使用生产媒体或凭据。此门禁不等于真实 PT、Web UI 人工点击、Transmission 同一任务全链路及正式 ARM64 发布/跨版本升级已获验证。

**同一任务真实 Transmission 全链路门禁已通过**：GitHub CI run `35448894436` 在原生 ARM64 隔离 Transmission 4.1.3 容器中，使用独立 128 MiB 合成媒体、模拟站点与模拟管理员，完成从分析、审批、执行计划、Hardlink 到不可跳过的客户端下载校验、做种和 DONE，且 LINK/ADD/VERIFY/START journal、幂等重放与源文件不变量检查通过。正式 ARM64 升级、真实 Web 审批及多平台发行身份仍为独立退出条件。

**Web 审批入口双下载器门禁已通过（GitHub CI run `35450823906`）**：审核页现可列出已启用且通过连接与路径门禁的 Transmission；目标选择或目标根与已加载执行计划不一致时必须重新生成计划，且 Transmission 必须显示客户端下载校验提示。新增前端单元与模拟 API 浏览器测试，并在真实 FastAPI TestClient 管理员会话中测试审批的 CSRF 保护、执行门、Transmission 目标计划和零文件/下载器副作用。CI 整体及全部五项 job 成功；浏览器与真实 HTTP 服务尚未连成同一个 E2E 会话，不能据此宣布 Web 人工实测、真实 PT 或正式 ARM64 升级/发布已完成。

**隔离浏览器真实 HTTP 审批门禁（新增，待新 CI）**：测试专用的 loopback FastAPI 与 Vite、Playwright 自动点击正式审核/分析组件完成合成任务只读分析、管理员审核、执行门与 Transmission 执行计划，实际读取持久化任务与计划并检查没有 journal/源文件或目标文件副作用。仅使用临时合成数据和不可连接的下载器配置；尚不覆盖正式 App 导航全流程、真人操作、真实 PT/客户端服务和 ARM64 正式发行或跨版本升级。

当前开发 Runner 没有 Docker daemon，但 GitHub Actions 已持续承担真实容器门禁。当前最新正式 Release 为 `v0.1.9`，Release workflow run `35429394091` 已成功完成正式发布，公开 GHCR 不可变镜像为 `ghcr.io/yyxiaoma/packbreaker@sha256:3bf7eee3825821a5fef28338b7f1ce749cbae353bcd3a4ef81883ee6a021261d`。当前源码保持 v0.1.9 版本线；真实 Docker 跨版本升级/回滚与 updater helper E2E 均已由正式 Release workflow 验证；浏览器 E2E 由发布前独立门禁完成。

## 2. 下载器

| 下载器 | 已验证版本 | 支持边界 |
| --- | --- | --- |
| qBittorrent | 5.2.3 / WebAPI 2.15.1 | 已完成认证、路径诊断、FULL_VERIFIED 主链、ADD/RECHECK/START/REMOVE journal、响应丢失恢复、release/rollback 真实验收。WebAPI 2.16.0 已移除 `skip_checking`，因此不能把 2.15.1 的 skip-check 语义直接外推到未来版本。 |
| Transmission | 4.1.3 | 已完成暂停添加、VERIFY、START、keep-data REMOVE、并发/响应丢失恢复和真实主链验收。 |

新增版本必须先通过只读连接/能力探测、路径映射、合成协议测试和至少一条受控真实链路；能力不匹配时失败关闭，不通过“尽量兼容”绕过执行安全门。

## 3. PT 站点

| 站点类型 | 当前支持边界 |
| --- | --- |
| M-Team | API Key；当前 API origin 与搜索/详情/取种链路已真实验收。 |
| HDTime | Cookie；NexusPHP 搜索、详情与 torrent 获取已有契约/真实验收。 |
| HHClub | Cookie；仅接受当前主站 `https://hhanclub.net`，新版 div 卡片搜索、`cat[]` 分类、详情与下载链路已有契约/真实验收。 |

v0.1.6 Profile Registry 另外列出以下**待适配**类型，但它们当前不能创建配置、保存凭证或执行临时 probe：KeepFrds（`https://pt.keepfrds.com`）、HDHome（`https://hdhome.org`）、UBits（`https://ubits.club`）、HDFans（`https://hdfans.org`）、BTSCHOOL（`https://pt.btschool.club`）、PTTime（`https://www.pttime.org`）和 Rousi Pro（`https://rousi.pro`）。前六项按 NexusPHP/Cookie profile 建模；Rousi Pro 暂归 `API_KEY` 凭证类型但仍无正式 Adapter。所有这些 profile 的 `support_status` 均为 `PENDING_ADAPTER`，只有完成 PackBreaker Adapter、契约测试与真实只读验收后才可扩大上表的正式支持集合。

站点凭证只写入加密 secret store，管理 API/UI 不回显已保存明文。站点临时故障、鉴权失败和限流不会成为放宽 torrent 内容验证的理由。

## 4. 数据与文件系统

- SQLite 是 v1.0 唯一数据库后端；Runtime 启动使用 Alembic head 校验和安全临时副本升级。
- `/config` 必须是可写真实目录并满足最小权限要求；`secret.key` 需要独立安全保管，不包含在数据库备份中。
- `/data` 作为媒体只读根；业务链不得修改源文件内容或替换源 inode。
- 零复制辅种依赖源与目标位于支持 hardlink 的同一设备；跨设备或证据不确定场景必须失败关闭或进入明确的人工/下载器校验流程。
- 受控 repair inode isolation 与自动 cleanup 额外要求目标文件系统支持 Linux `user.*` extended attributes。PackBreaker 会在独立 repair target 上持久化 `user.packbreaker.repair_owner` ownership marker；文件系统不支持 xattr、marker 缺失或 marker 与 isolation journal 不一致时必须失败关闭，不能仅凭 inode/size 推断所有权。普通 hardlink 辅种不依赖该 marker。
- 路径映射采用明确 remote/container 前缀，禁止路径穿越、符号链接逃逸和未证明目标目录。

## 5. 升级与回滚

- 当前数据库 head 为 `0029_v018_compatibility`；自动化矩阵覆盖所有历史 revision（包括 `0024_task_center_v015`）到当前 head。
- 生产升级使用不可变 `<image>@sha256:<digest>`；`stable` 只用于发现，不是部署身份。
- 数据库升级先创建 `pre-upgrade` 一致性快照，在同文件系统临时副本完成迁移与验证后再原子切换。
- 生产回滚不依赖 Alembic 原地 downgrade；旧镜像不能读取新 schema 时必须恢复兼容的升级前/离线备份。
- `v0.1.2` Release workflow run `34937718889` 已真实跑绿 `v0.1.1` 基线启动/备份 → `v0.1.2` 候选接管/readiness → 用 `v0.1.1` 镜像恢复旧备份 → `v0.1.1` 再次 readiness，并额外通过独立 updater helper 的真实成功升级与故障候选自动数据库/容器回滚。
- `release-baseline.json` 当前固定正式 `v0.1.9` digest；后续候选版本必须以该最新 published baseline 做相邻版本升级/回滚门禁后才允许进入正式发布。
- `v0.1.2` 正式提供独立 updater helper 的 Web 一键升级链路；`v0.1.7` 起正式支持显式挂载 docker.sock 的 Compose 单容器使用同一 Web 升级链，并保留 Compose labels。自动容器替换仍只承诺单个 PackBreaker 容器、唯一可写 `/config`、官方 GHCR 镜像、可安全重建的端口/环境/挂载/restart policy 和单网络配置。复杂 namespace、多网络、显式静态 IP/MAC 或 AutoRemove 容器继续失败关闭。

## 6. 兼容承诺原则

支持矩阵只会在自动化协议证据与必要的真实环境证据同时满足后扩大。未验证的新下载器版本、站点实现、CPU 架构或文件系统不会被静默视为兼容；无法证明时一律保持人工确认、阻断或只读诊断。
