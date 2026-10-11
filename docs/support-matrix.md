# PackBreaker 支持矩阵（更新于 2026-10-11）

本页区分**正式发布、代码可用、真实只读探测、独立内容/辅种全链路验收**。一个站点可配置、连接成功或有客户端既有种子，都不等于正式完成该站的端到端业务验收。

- **最新正式 Release：v1.1.1**。源码提交 `b779e3270f2a3b3d8c380e22ac36f06ce55f8252`；不可变 GHCR index 为 `sha256:839daecddf8c3d60566b688b2016d47d84935c4b35da8cd2028aab073836def9`。
- **最近一次已交付 v1.1.2 候选（非正式 Release）**：源码 `e784c194a4af19f21b6fbce4ec4215f6b99fa444`，标签 `candidate-v1.1.2-e784c194a4af`；GHCR / Docker Hub 的不可变多架构摘要均为 `sha256:b6289c9ebf311afe9a1ce9f49eb1eefb854d4885b7177b3d4065416a7cfcdc6c`（[Actions #38025511243](https://github.com/YYxiaoma/PackBreaker/actions/runs/38025511243)），两仓库原生 ARM64/AMD64 验证成功。该候选早于后续代码，不能当作最新整合产物。
- **当前合并后研发源码**：`main@4165edaa019443aba8e5dd03632cfe0bd0e367b2`，PR #22 已包含 Rousi/匹配恢复和 PR #21 等价离线安全补丁；PR #21 作为重复草稿关闭。该主线的 [Candidate Docker E2E](https://github.com/YYxiaoma/PackBreaker/actions/runs/38109131714) 已成功，[CI](https://github.com/YYxiaoma/PackBreaker/actions/runs/38109131711) 须以最终状态为准。**尚无该最新版双注册表不可变候选镜像，不能混用旧 digest。**
- **v1.1.2 收官门槛**：[Issue #5](https://github.com/YYxiaoma/PackBreaker/issues/5)。无需 Synology NAS 现场验收，未经授权不操作生产 NAS。

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

## 2. 当前 11 个内置站点（代码可启用）

| 站点 | 认证 | 状态和仍待验收的边界 |
| --- | --- | --- |
| M-TEAM | API Key | 代码可用，既有搜索/认证证据，具体目标任务仍须独立校验 |
| HDTime | Cookie | 代码可用，NexusPHP 搜索/连接真实样本已验证 |
| HHClub | Cookie | 代码可用，固定主站及 NexusPHP 真实连接证据 |
| KeepFrds | Cookie | 代码可用；最新只读单页有一个严格候选且详情身份匹配，完整 PackBreaker 任务 E2E 未验收 || HDHome | Cookie | 代码可用；目标影片有可行候选，但 PackBreaker 全链路未单独关闭 |
| UBits | Cookie | 代码可用；间歇连接失败待复核。Transmission Tracker 状态条目 460 个，其中 379 最近成功、81 失败（64 超时）；不是独立任务数 |
| HDFans | Cookie | 隔离审计中用户新候选完整 Piece **7,321/7,321** 通过；已有同 hash Transmission 做种任务无 PackBreaker ownership journal，禁止重复添加或认领；正式任务链未完成 |
| BTSCHOOL | Cookie | 两个严格候选存在歧义；Tracker 状态条目 708 个，其中 705 最近成功、3 失败（2 超时） |
| PTTime | Cookie | 搜索及详情身份已验证；用户新候选隔离全 Piece **7,321/7,321** 通过，但源私种未导入 Runner 的正式任务链，下载器验证未完成 |
| Rousi Pro | API Key 搜索 + 独立下载 Cookie | 双凭据必须分别保护；临时只读故障分类和冷却逻辑已有代码修复，Tracker 最近 3 条状态均失败，根因仍待定位 || 聆音Club | Cookie | 代码可用；目标影片有可行候选，完整正式任务仍需核对 |

以上站点“代码可启用”的含义是 Adapter 和任务入口已实现，**不是**每站都在独立正式 PackBreaker 任务中完成了真实取种、媒体校验、文件操作、下载器做种和恢复测试。

## 3. 七个新增站点（仅可配置及只读探测）

| 站点 | Registry 状态 | 已有真实证据 / 必须补齐 |
| --- | --- | --- |
| PTerClub | `PENDING_REAL_VALIDATION` | 2026-10-10 真实搜索 23 条、1 条可行候选；详情阶段 `SITE_UNAVAILABLE`；仍缺合法取种与独立内容校验 |
| Audiences | `PENDING_REAL_VALIDATION` | 十列搜索数值映射已修复，真实单页日期/容量/做种/下载数 **16/16** 可解析；当前参考影片无严格候选，仍缺合法取种和完整 Piece || SpringSunday | `PENDING_REAL_VALIDATION` | 2026-10-10 真实解析 16 条搜索结果，当前影片无可行候选；历史官方连接超时仍需独立排查 |
| HDDolby | `PENDING_REAL_VALIDATION` | 历史搜索曾返回结果；2026-10-10 单次搜索报 `SITE_UNAVAILABLE`，合适目标与独立 Piece 验收仍缺失 |
| U2 | `PENDING_REAL_VALIDATION` | 既有 19/19 动画搜索及详情身份已验证；2026-10-10 另一个动画查询返回 50 条，大小、做种、下载数均 50/50 可解析，但有下一页；间歇连接错误、合法取种及独立 Piece 仍待验收 |
| 不可躺（TANGPT） | `PENDING_REAL_VALIDATION` | 2026-10-10 搜索返回 5 条，当前影片 2 条可行候选仍有歧义，不得自动任选其一 |
| CarPT | `PENDING_REAL_VALIDATION` | 已有唯一严格候选和详情身份证据；用户提供的新候选在隔离审计中完整 Piece **1,831/1,831** 通过；正式站点支持和 PackBreaker 任务链仍未验收 |
七站的加密配置和只读连接测试**不授权启用正式任务**。即使有人手动将数据库记录设为启用，也不得解密凭据进入生产适配链；只有逐站通过认证、同源保护、契约、错误分类及受控真实验证后才能修改 Registry 的正式支持范围。

## 4. 真实验收证据与发布边界

独立 WebCodex 8000 端口审核环境曾通过真实 Transmission RPC 识别目标影片的 13 个不同 hash，39/39 文件大小一致，13/13 主视频与源目录同设备及 inode，全部报告 `have_valid == size_when_done`、`have_unchecked == 0`。这是**既有种子客户端状态**，不证明这些任务由 PackBreaker 创建，更不能代替新候选的 torrent metainfo 和 Piece Hash 独立验证。无 ownership journal 证据，绝不重复添加或认领同 hash 种子。

### 2026-10-10 只读 Tracker、内容校验与代码门禁证据

独立审核环境对 Transmission 的只读 `torrent_get(tracker_stats)` 返回 6,744 条客户端任务。按 **Tracker 状态条目**（不是独立 Torrent 或 PackBreaker 新建任务）计数：

| 站点 | 状态条目 | 最近成功 | 最近失败 | 失败中超时 |
| --- | ---: | ---: | ---: | ---: |
| BTSCHOOL | 708 | 705 | 3 | 2 |
| Rousi Pro | 3 | 0 | 3 | 0 |
| UBits | 460 | 379 | 81 | 64 |

这份快照与 2026-10-09 的单任务读数采用不同统计范围，不得相加、外推故障根因或泄露 Tracker URL、passkey 和真实种子身份。RC1 已整合 PR #11–#21 对应的匹配可靠性、只读故障、未知写结果对账与离线安全检查代码；本地关键专项测试与静态门禁已通过，**最终候选镜像与真实业务 E2E 仍未关闭；PR #21 已由 PR #22 覆盖并关闭**。

用户提供的 CarPT、HDFans、PTTime 三份私种与一组真实源媒体在隔离审计中完成全 Piece 内容核对，分别为 **1,831/1,831、7,321/7,321、7,321/7,321**。通过跨环境 Piece-SHA1 序列承诺值比较；原始私种未进入 WebCodex Runner，所以不能记作当前 Runner 工具读取演练、正式任务、客户端 verify 或 ownership journal 验收。[审计证据](https://github.com/YYxiaoma/PackBreaker/issues/5#issuecomment-6095145394)。HDFans 已有外部同 hash 客户端任务，必须维持只读。

当前所需现场补验、失败分类与退出条件维护于 [Issue #5](https://github.com/YYxiaoma/PackBreaker/issues/5) 和 [v1.1.1 的真实只读记录](./v1.1.1-development.md)。历史 v1.0.0～v1.0.5 发行记录与旧的测试范围可通过 Git 历史和 `docs/v1.0.*-release.md` 查阅，不再视为本页“当前版本”说明。
