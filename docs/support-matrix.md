# PackBreaker 支持矩阵（更新于 2026-10-14）

**当前代码支持 18 个站点：原有 11 站及新增七站均为 `SUPPORTED`（Adapter 已接入并可用于正常拆包任务）。** 新站仍需在站点管理中配置凭证并显式启用；CookieCloud 可为七个 Cookie 站点按固定 HTTPS Profile 域名自动导入或更新凭证，新导入配置默认停用。手动/监控拆包正常执行还取决于源文件、候选 Torrent、完整内容验证、冻结计划、下载器路径/归属与操作权限。

**`SUPPORTED` 只表示代码适配与任务准入，不代表七站独立真实 E2E 已通过。** 正式发布的七站 E2E 收据仍是 0/7；独立收据及发行授权门禁保持不变。参见 [用户范围决策](./v1.1.2-user-acceptance-decision-20261014.md) 和 [Issue #5](https://github.com/YYxiaoma/PackBreaker/issues/5)。

以下 2026-10-12 的记录保留为**历史证据快照**，其中对 Registry 状态及候选镜像“最新”的说法不再代表当前代码事实。

### 2026-10-12 历史实测记录（保留当时证据）

- **U2：用户批准的受限真实性验收通过**，在缺少匹配动画媒体的前提下，真实 Cookie 认证、用户资料只读 GET、两页分页搜索各 50 条（共 100 条，每页大小/做种/下载数/发布时间均 50/50）、首条详情的站点与种子身份匹配，以及用户提交的 private v1 / 25 文件 / 3,272 Piece 种子元信息解析均成功；合成 NexusPHP/候选分页契约回归成功。用户本轮明确豁免 U2 的媒体 Piece 和客户端下载器 E2E，故标记为**受限范围验收通过（带豁免）**，绝不能声称这两项已实测或将 Registry 自动改为正式支持。
- **Audiences、不可躺：独立原媒体全 Piece 通过**，分别 **7,321/7,321** 和 **1,831/1,831**，源快照未变化。Transmission 中已有两份同 hash 的现存任务，均在完整做种且有历史做种时间。用户希望删除后重加，但客户端公开 RPC 不能无损恢复原任务添加时间/做种历史及其他内部统计；本轮为保护原始做种未删除、停止、重检或认领它们。仍缺来自 PackBreaker 正式冻结计划及 journal 的受控任务链，而非内容一致性。
- **SpringSunday：新的合法 66 GB 原种及真实内容全 Piece 已通过**。用户最初给的路径多一层 `downloads2`；原 `[SSD]` 文件对应 20,719,045,435 字节版本，不能硬套在现有 66,033,395,661 字节影片上。依据真实站点页面唯一同容量候选、同站详情身份（详情未列出容量）、一次合法 `fetch_torrent` 取得的原始 private torrent，**精确文件大小和文件名均匹配**；独立读取该 66 GB 原媒体完成 **3,936/3,936 Piece FULL_VERIFIED**，零失配，源快照不变。TR 已有完全相同 InfoHash 的 SpringSunday 做种任务，`have_valid == size_when_done`、`have_unchecked=0`、100% 正在做种。保护既有任务，没有重新添加/停止/删除；仍缺正式 PackBreaker 新任务计划与 journal 全链路。
- **候选代码证据**：GitHub [Draft PR #32](https://github.com/YYxiaoma/PackBreaker/pull/32) 源码树与整合后的本地 SHA 完全一致；[CI #38215842378](https://github.com/YYxiaoma/PackBreaker/actions/runs/38215842378) 共 **arm64、quality、browser-e2e、container、updater-e2e 五个 Job 全部成功**。本地合并后端 **1,762 passed、4 skipped**，前端 65 passed，生产构建及静态门禁通过。此处不是完整七站 PackBreaker 真实业务 E2E、不是双仓库最终候选镜像交付，也不是正式 Release。
- **当时的发布门禁**：2026-10-12 七新增站的 Registry 仍保持 `PENDING_REAL_VALIDATION`；2026-10-14 起源码已改为 `SUPPORTED`，但真实 E2E 发布门禁仍未放开。U2 经用户同意的例外验收要在候选/正式支持决策中单独标注。其余站点仍需完成从站点合法取种、冻结计划、独立媒体落位、operation journal 到客户端恢复的完整业务链；不得把本次只读 Piece 或直接客户端测试与该业务 E2E 混同。


- **最新正式 Release：v1.1.1**。源码提交 `b779e3270f2a3b3d8c380e22ac36f06ce55f8252`；不可变 GHCR index 为 `sha256:839daecddf8c3d60566b688b2016d47d84935c4b35da8cd2028aab073836def9`。
- **最近一次已交付 v1.1.2 候选（非正式 Release）**：源码 `e784c194a4af19f21b6fbce4ec4215f6b99fa444`，标签 `candidate-v1.1.2-e784c194a4af`；GHCR / Docker Hub 的不可变多架构摘要均为 `sha256:b6289c9ebf311afe9a1ce9f49eb1eefb854d4885b7177b3d4065416a7cfcdc6c`（[Actions #38025511243](https://github.com/YYxiaoma/PackBreaker/actions/runs/38025511243)），两仓库原生 ARM64/AMD64 验证成功。该候选早于后续代码，不能当作最新整合产物。
- **2026-10-12 候选提交前审计基线**：`candidate/v1.1.2-rc1@fe6315d`，其上有 **12 个待提交的本地修改文件**，包含辅助文件只读超时、HDDolby 详情超时、最终辅种 journal / checkpoint 多 worker 安全修复以及隔离测试。工作树不是不可变源码 SHA，**不能用历史候选镜像代表这些未提交修复**；尚未对该最终源码重新交付镜像或正式发布。
- **v1.1.2 收官门槛**：[Issue #5](https://github.com/YYxiaoma/PackBreaker/issues/5)。用户取消 Synology NAS 现场验收要求，但仍不授权 NAS 生产写入；七新增站全部真实验收和正式发布需独立通过。

## 1. 平台与下载器

| 能力 | 支持情况 | 真实证据边界 |
| --- | --- | --- |
| Linux `amd64` | 正式支持 | v1.1.1 不可变发行镜像；候选相邻升级、回滚与 updater CI 通过 |
| Linux `arm64` / aarch64 | 正式支持 | v1.1.1 原生双架构发行；最新 v1.1.2 双仓库候选已通过原生 ARM64 对等运行验证 |
| ARMv7、其他 Linux 架构 | 未承诺 | 缺少相应的发行镜像与验收 |
| Windows/macOS 原生部署 | 不属于正式生产部署范围 | 可用于研发 |
| qBittorrent 5.2.3 | 已实现协议接入 | 隔离真实客户端 ADD/RECHECK/START、幂等及失败回滚已通过；未来 WebAPI 能力必须重新判定 |
| Transmission 4.1.3 | 已实现协议接入 | 隔离真实客户端 ADD/VERIFY/START、journal 已通过；真实现场的既有做种不代表 PackBreaker 新任务完成 |

源路径允许显式挂载并授权的 `/downloads`、`/downloads2` 等绝对路径，不再强制在 `/data` 下。跨设备硬链接、符号链接逃逸、未证实目标 ownership、无法完成 Piece 校验时必须阻断；下载器校验语义不可互相代替。

## 2. 原有 11 个内置站点（代码可启用）

| 站点 | 认证 | 状态和仍待验收的边界 |
| --- | --- | --- |
| M-TEAM | API Key | 代码可用，既有搜索/认证证据，具体目标任务仍须独立校验 |
| HDTime | Cookie | 代码可用，NexusPHP 搜索/连接真实样本已验证 |
| HHClub | Cookie | 代码可用，固定主站及 NexusPHP 真实连接证据 |
| KeepFrds | Cookie | 代码可用；最新只读单页有一个严格候选且详情身份匹配，完整 PackBreaker 任务 E2E 未验收 |
| HDHome | Cookie | 代码可用；目标影片有可行候选，但 PackBreaker 全链路未单独关闭 |
| UBits | Cookie | 代码可用；间歇连接失败待复核。Transmission Tracker 状态条目 460 个，其中 379 最近成功、81 失败（64 超时）；不是独立任务数 |
| HDFans | Cookie | 隔离审计中用户新候选完整 Piece **7,321/7,321** 通过；已有同 hash Transmission 做种任务无 PackBreaker ownership journal，禁止重复添加或认领；正式任务链未完成 |
| BTSCHOOL | Cookie | 两个严格候选存在歧义；Tracker 状态条目 708 个，其中 705 最近成功、3 失败（2 超时） |
| PTTime | Cookie | 搜索及详情身份已验证；用户新候选隔离全 Piece **7,321/7,321** 通过，但源私种未导入 Runner 的正式任务链，下载器验证未完成 |
| Rousi Pro | API Key 搜索 + 独立下载 Cookie | 双凭据必须分别保护；临时只读故障分类和冷却逻辑已有代码修复，Tracker 最近 3 条状态均失败，根因仍待定位 |
| 聆音Club | Cookie | 代码可用；目标影片有可行候选，完整正式任务仍需核对 |

以上站点“代码可启用”的含义是 Adapter 和任务入口已实现，**不是**每站都在独立正式 PackBreaker 任务中完成了真实取种、媒体校验、文件操作、下载器做种和恢复测试。

## 3. 七个新增站点（代码适配已完成、可启用正常任务）

| 站点 | 当前代码状态 | 历史真实证据与尚缺的收据 |
| --- | --- | --- |
| PTerClub | `SUPPORTED` | 真实认证、搜索及解析已有证据；尚无新 PackBreaker Web execution 的完整 E2E 收据 |
| Audiences | `SUPPORTED` | 搜索、内容 Piece 验证和隔离客户端子链已有证据；正式新站任务全链回执仍缺 |
| SpringSunday | `SUPPORTED` | 66 GB 合法原种与 3,936/3,936 Piece 通过，已有外部同 hash 任务不可接管；完整新任务 E2E 仍缺 |
| HDDolby | `SUPPORTED` | 搜索/详情曾通过，站点间歇超时需后续版本继续复核；完整新任务 E2E 仍缺 |
| U2 | `SUPPORTED` | 真实只读搜索/详情与 Torrent 解析已有用户受限范围批准；媒体/客户端下载器环节未实测 |
| 不可躺（TANGPT） | `SUPPORTED` | 独立 Piece 与隔离客户端子链已有证据；正式新站任务全链回执仍缺 |
| CarPT | `SUPPORTED` | 独立 Piece 与客户端子链已分别测试；此前旧 Web execution 被永久冻结为只读，不能作为新的可写 E2E 收据 |

七站的正常执行由显式启用的站点配置、任务准入、真实文件/下载器权限及全部业务门禁控制。**旧只读 `acceptance_mode` 执行不得解冻；只能由正常新 execution 进入生产 Worker，且不保证站点在线或目标媒体适配。**


## 4. 真实验收证据与发布边界

独立 WebCodex 8000 端口审核环境曾通过真实 Transmission RPC 识别目标影片的 13 个不同 hash，39/39 文件大小一致，13/13 主视频与源目录同设备及 inode，全部报告 `have_valid == size_when_done`、`have_unchecked == 0`。这是**既有种子客户端状态**，不证明这些任务由 PackBreaker 创建，更不能代替新候选的 torrent metainfo 和 Piece Hash 独立验证。无 ownership journal 证据，绝不重复添加或认领同 hash 种子。

### 2026-10-10 只读 Tracker、内容校验与代码门禁证据

独立审核环境对 Transmission 的只读 `torrent_get(tracker_stats)` 返回 6,744 条客户端任务。按 **Tracker 状态条目**（不是独立 Torrent 或 PackBreaker 新建任务）计数：

| 站点 | 状态条目 | 最近成功 | 最近失败 | 失败中超时 |
| --- | ---: | ---: | ---: | ---: |
| BTSCHOOL | 708 | 705 | 3 | 2 |
| Rousi Pro | 3 | 0 | 3 | 0 |
| UBits | 460 | 379 | 81 | 64 |

这份快照与 2026-10-09 的单任务读数采用不同统计范围，不得相加、外推故障根因或泄露 Tracker URL、passkey 和真实种子身份。当前代码已包含匹配可靠性、只读故障、未知写入对账及离线安全修复；`67ff480e` 候选镜像及 CI 已交付通过，但**真实任务 E2E 仍未关闭**。PR #21 已由 PR #22 等效覆盖并关闭。

用户提供的 CarPT、HDFans、PTTime 三份私种与一组真实源媒体在隔离审计中完成全 Piece 内容核对，分别为 **1,831/1,831、7,321/7,321、7,321/7,321**。通过跨环境 Piece-SHA1 序列承诺值比较；原始私种未进入 WebCodex Runner，所以不能记作当前 Runner 工具读取演练、正式任务、客户端 verify 或 ownership journal 验收。[审计证据](https://github.com/YYxiaoma/PackBreaker/issues/5#issuecomment-6095145394)。HDFans 已有外部同 hash 客户端任务，必须维持只读。

用户提供的 CarPT、HDFans、PTTime 三份私种与一组真实源媒体在隔离审计中完成全 Piece 内容核对，分别为 **1,831/1,831、7,321/7,321、7,321/7,321**。通过跨环境 Piece-SHA1 序列承诺值比较；原始私种未进入 WebCodex Runner，所以不能记作当前 Runner 工具读取演练、正式任务、客户端 verify 或 ownership journal 验收。[审计证据](https://github.com/YYxiaoma/PackBreaker/issues/5#issuecomment-6095145394)。HDFans 已有外部同 hash 客户端任务，必须维持只读。
当前所需现场补验、失败分类与退出条件维护于 [Issue #5](https://github.com/YYxiaoma/PackBreaker/issues/5) 和 [v1.1.1 的真实只读记录](./v1.1.1-development.md)。历史 v1.0.0～v1.0.5 发行记录与旧的测试范围可通过 Git 历史和 `docs/v1.0.*-release.md` 查阅，不再视为本页“当前版本”说明。

> **2026-10-11 续验**：PTerClub 搜索字段 27/27、HDDolby 45 秒只读搜索四字段 9/9，SpringSunday 认证成功但搜索超时；Tracker 后续只读快照和开放事项请查阅 [最新收官快照](./v1.1.2-gate-snapshot-20261011.md)。上表保留 2026-10-10 历史快照，不能当作实时状态。
