# PackBreaker 支持矩阵（更新于 2026-10-09）

本页区分**正式发布、代码可用、真实只读探测、独立内容/辅种全链路验收**。一个站点可配置、连接成功或有客户端既有种子，都不等于正式完成该站的端到端业务验收。

- **最新正式 Release：v1.1.1**。源码提交 `b779e3270f2a3b3d8c380e22ac36f06ce55f8252`；不可变 GHCR index 为 `sha256:839daecddf8c3d60566b688b2016d47d84935c4b35da8cd2028aab073836def9`。
- **研发候选：v1.1.2**。阶段性双注册表候选 `candidate-v1.1.2-24f0213b07a1` 已通过 AMD64、原生 ARM64、升级回滚和镜像摘要检查（[Actions #37948200449](https://github.com/YYxiaoma/PackBreaker/actions/runs/37948200449)）；不是已正式发布的 v1.1.2，也不能代替尚缺的 PT 真实验收。后续改动必须重新构建最终候选。
- **v1.1.2 收官门槛**：[Issue #5](https://github.com/YYxiaoma/PackBreaker/issues/5)。用户已明确取消 Synology NAS 现场验收要求；不对 NAS 生产环境进行任何未经授权的操作。

## 1. 平台与下载器

| 能力 | 支持情况 | 真实证据边界 |
| --- | --- | --- |
| Linux `amd64` | 正式支持 | v1.1.1 不可变发行镜像；候选相邻升级、回滚与 updater CI 通过 |
| Linux `arm64` / aarch64 | 正式支持 | v1.1.1 原生双架构发行；v1.1.2 阶段性候选原生 ARM64 运行通过 |
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
| KeepFrds | Cookie | 代码可用；当前指定影片的搜索没有合适候选，不应宣称全链路通过 |
| HDHome | Cookie | 代码可用；目标影片有可行候选，但 PackBreaker 全链路未单独关闭 |
| UBits | Cookie | 代码可用；目标影片有可行候选、偶发连接超时；最近一次 Tracker announce 已恢复成功 |
| HDFans | Cookie | 代码可用；既有 Transmission 种子 7,321 个 Piece 全可用，**新候选独立 Piece Hash** 仍未验证 |
| BTSCHOOL | Cookie | 代码可用；两条大小相关候选和详情身份已确认，另有一条 Tracker announce 超时 |
| PTTime | Cookie | 代码可用；六条搜索结果中有唯一可行候选、详情身份正常；新候选独立内容校验仍未完成 |
| Rousi Pro | API Key 搜索 + 独立下载 Cookie | 双凭据必须分别保护；当前仍有一条 Tracker announce 未确认成功 |
| 聆音Club | Cookie | 代码可用；目标影片有可行候选，完整正式任务仍需核对 |

以上站点“代码可启用”的含义是 Adapter 和任务入口已实现，**不是**每站都在独立正式 PackBreaker 任务中完成了真实取种、媒体校验、文件操作、下载器做种和恢复测试。

## 3. 七个新增站点（仅可配置及只读探测）

| 站点 | Registry 状态 | 已有真实证据 / 必须补齐 |
| --- | --- | --- |
| PTerClub | `PENDING_REAL_VALIDATION` | 2026-10-10 真实搜索 23 条，当前影片有 1 条可行候选；历史超时仍需关注，未完成取种及独立内容校验 |
| Audiences | `PENDING_REAL_VALIDATION` | 历史搜索有结果，但当前影片无大小合适资源；2026-10-10 单次搜索报 `SITE_UNAVAILABLE`，未自动重试 |
| SpringSunday | `PENDING_REAL_VALIDATION` | 2026-10-10 真实解析 16 条搜索结果，当前影片无可行候选；历史官方连接超时仍需独立排查 |
| HDDolby | `PENDING_REAL_VALIDATION` | 历史搜索曾返回结果；2026-10-10 单次搜索报 `SITE_UNAVAILABLE`，合适目标与独立 Piece 验收仍缺失 |
| U2 | `PENDING_REAL_VALIDATION` | 既有 19/19 动画搜索及详情身份已验证；2026-10-10 另一个动画查询返回 50 条，大小、做种、下载数均 50/50 可解析，但有下一页；间歇连接错误、合法取种及独立 Piece 仍待验收 |
| 不可躺（TANGPT） | `PENDING_REAL_VALIDATION` | 2026-10-10 搜索返回 5 条，当前影片 2 条可行候选仍有歧义，不得自动任选其一 |
| CarPT | `PENDING_REAL_VALIDATION` | 2026-10-10 搜索返回 7 条，当前影片 1 条可行，历史详情 ID 一致；独立取种和内容验证待补 |

七站的加密配置和只读连接测试**不授权启用正式任务**。即使有人手动将数据库记录设为启用，也不得解密凭据进入生产适配链；只有逐站通过认证、同源保护、契约、错误分类及受控真实验证后才能修改 Registry 的正式支持范围。

## 4. 真实验收证据与发布边界

独立 WebCodex 8000 端口审核环境曾通过真实 Transmission RPC 识别目标影片的 13 个不同 hash，39/39 文件大小一致，13/13 主视频与源目录同设备及 inode，全部报告 `have_valid == size_when_done`、`have_unchecked == 0`。这是**既有种子客户端状态**，不证明这些任务由 PackBreaker 创建，更不能代替新候选的 torrent metainfo 和 Piece Hash 独立验证。无 ownership journal 证据，绝不重复添加或认领同 hash 种子。

当前所需现场补验、失败分类与退出条件维护于 [Issue #5](https://github.com/YYxiaoma/PackBreaker/issues/5) 和 [v1.1.1 的真实只读记录](./v1.1.1-development.md)。历史 v1.0.0～v1.0.5 发行记录与旧的测试范围可通过 Git 历史和 `docs/v1.0.*-release.md` 查阅，不再视为本页“当前版本”说明。
