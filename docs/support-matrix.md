# PackBreaker v1.0 支持矩阵

本页定义当前明确支持或已经现场验证的运行边界。未列为“支持”的组合不等于一定不可用，但在取得对应协议、恢复和真实环境证据前不得作为正式兼容承诺。

## 1. 发布平台

| 项目 | 当前状态 | 说明 |
| --- | --- | --- |
| Linux `amd64` 容器 | v1.0.0 正式发布 | 正式镜像含 `linux/amd64`；保留上一正式 v0.1.9 不可变摘要的升级/回滚与备份恢复证据。 |
| Linux `arm64` / aarch64 容器 | v1.0.0 正式发布 | 正式镜像含 `linux/arm64`，已通过原生 ARM64 与隔离 QEMU 的**同一已发布不可变摘要**启动、预检及数据库备份；真实生产 PT/下载器现场接管另需用户环境验收。 |
| Linux 其他架构（含 ARMv7） | 未声明支持 | 尚无正式镜像及相应构建、恢复验收矩阵。 |
| Windows / macOS 原生生产运行 | 未声明支持 | 可用于开发，但 v1.0 生产部署以 Linux 容器为边界。 |

**v1.0.0 正式双架构发布证据**：[受控恢复发布 run `35507181886`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35507181886) 已在三个独立 Runner 上完成相同 GHCR index digest 的 AMD64 相邻升级/回滚、原生 ARM64 及 QEMU ARM64 运行，并经 GitHub Release 资产上传/下载回读及 `stable/latest` 摘要检查。正式 index 为 `sha256:fce1f3e3c0f56a8c024bbbf46a65fbc4ca51225a5c2bc2543fdddbb02c21a0c4`；v0.1.9 没有发布 ARM64 镜像，因此不存在 `v0.1.9 ARM64 → v1.0.0 ARM64` 的正式跨版本升级证据。ARM64 合成数据和隔离客户端下载器链路的研发测试结果保留如下；**不代表已连接用户真实 PT 或生产下载器完成 ARM64 现场验收**。ARMv7 不包含在正式支持范围内，详见 [研发路线图](./development-roadmap.md)。

ARM64 业务测试范围：GitHub CI run `35434318531` 的原生 ARM64 job 已通过真实 ARM64 Docker 容器的隔离 v1/v2/hybrid 媒体读取、映射、FULL_VERIFIED 与损坏降级、合成文件 Hardlink 及只读源不变量检查；原生 ARM64 Runner 的 qBittorrent/Transmission **模拟适配器**、任务 journal 和 linking 回归也已通过。此证据不代表真实 qBittorrent/Transmission 服务在 ARM64 环境下完成端到端验收，亦不代表正式 ARM64 跨版本升级或发行镜像验收。

**隔离真实下载器 API 门禁已通过**：GitHub CI run `35436525382` 的原生 ARM64 job 已成功运行独立无外网 Docker qBittorrent 5.2.3 / Transmission 4.1.3 真实协议测试（仅合成种子和一次性配置），以及 repair inode 隔离、xattr 归属和 journal 崩溃恢复回归；qBittorrent 添加请求已通过提交 `a0b37db` 同时设置 `paused=true` 和 `stopped=true` 并验证任务真实停止状态。此项不是连接真实 PT/生产下载器的 E2E、完整任务生命周期或正式 ARM64 跨版本升级证据；不接触用户真实下载器或媒体。

**ARM64 已授权任务连续链路已通过**：GitHub CI run `35439015971` 已通过独立无外网 qBittorrent 容器上的真实应用层 ADDING、SEEDING、ADD/START journal 和重放机制，检查服务端 `DONE` 与真实客户端状态及文件 inode 一致性。run `35440660113` 已通过独立无外网 Transmission 容器中的 ADDING、CLIENT_VERIFYING、SEEDING、ADD/VERIFY/START journal 和源文件不变测试；该测试使用专用的 128 MiB 合成媒体以获得真实客户端校验过程的可观察证据。前置 ANALYZING/LINKING、Web UI 审批、正式跨版本升级和正式双架构发布仍需分别取得证据，不能把已授权任务切片扩大声明为全部正式支持。

Transmission 的较大合成样本曾暴露自动校验期间首次停止请求尚未收敛的竞态（CI run `35440957090`）；提交 `05431a9` 仅在确认 torrent 身份、归属与保存路径不变时限时只读等待停止状态，不重发外部写操作，超时仍要求对账。原生 ARM64 CI run `35442764543` 和独立 Candidate Docker E2E run `35442764564` 已通过修复后的门禁；该证据仅覆盖隔离已授权任务链，正式 ARM64 跨版本升级和完整前置任务生命周期仍未验收。

**已批准计划 → LINKING → 真实下载器门禁已通过**：GitHub CI run `35443372973` 在原生 ARM64 环境中，从 CI 合成的已批准 Execution Plan 交给正式 LINKING 协调器创建 Hardlink 与文件操作 journal，接续 qBittorrent / Transmission 的隔离真实客户端添加、校验、做种和幂等恢复；前置分析、审批动作和执行计划生成过程尚未在同一个真实客户端任务中贯通，不据此宣称全生命周期或正式 ARM64 支持。

**前置生命周期门禁已通过**：GitHub CI run `35446449569` 在原生 ARM64 Runner 上使用独立合成任务调用正式分析、审核、执行门、计划生成与 LINKING 服务，验证候选 FULL_VERIFIED、审批越权拒绝、无副作用计划及 Hardlink journal 幂等；站点、审批主体和目标下载器配置均为测试合成对象，并非真实 Web 人工操作或真实站点/下载器。与上述两种真实下载器后置切片尚未合并为同一任务的完整端到端验收。

**同一任务真实 qBittorrent 全链路门禁已通过**：GitHub CI run `35447763395` 在原生 ARM64 隔离 qBittorrent 测试中，以合成源文件/模拟站点和模拟管理员审批身份创建真实的任务、审核、执行计划及 Hardlink/ADD/START journal，并在同一任务中经真实客户端收敛到 DONE，含未授权失败关闭、重放与源文件安全检查；合成种子使用与前一项测试不同的信息哈希以防止误测已有任务，不使用生产媒体或凭据。此门禁不等于真实 PT、Web UI 人工点击、Transmission 同一任务全链路及正式 ARM64 发布/跨版本升级已获验证。

**同一任务真实 Transmission 全链路门禁已通过**：GitHub CI run `35448894436` 在原生 ARM64 隔离 Transmission 4.1.3 容器中，使用独立 128 MiB 合成媒体、模拟站点与模拟管理员，完成从分析、审批、执行计划、Hardlink 到不可跳过的客户端下载校验、做种和 DONE，且 LINK/ADD/VERIFY/START journal、幂等重放与源文件不变量检查通过。正式 ARM64 升级、真实 Web 审批及多平台发行身份仍为独立退出条件。

**Web 审批入口双下载器门禁已通过（GitHub CI run `35450823906`）**：审核页现可列出已启用且通过连接与路径门禁的 Transmission；目标选择或目标根与已加载执行计划不一致时必须重新生成计划，且 Transmission 必须显示客户端下载校验提示。新增前端单元与模拟 API 浏览器测试，并在真实 FastAPI TestClient 管理员会话中测试审批的 CSRF 保护、执行门、Transmission 目标计划和零文件/下载器副作用。CI 整体及全部五项 job 成功；浏览器与真实 HTTP 服务尚未连成同一个 E2E 会话，不能据此宣布 Web 人工实测、真实 PT 或正式 ARM64 升级/发布已完成。

**隔离浏览器真实 HTTP 审批门禁已通过（GitHub CI run `35452724809`）**：测试专用的 loopback FastAPI 与 Vite、Playwright 自动点击正式审核/分析组件完成合成任务只读分析、管理员审核、执行门与 Transmission 执行计划，实际读取持久化任务与计划并检查没有 journal/源文件或目标文件副作用。仅使用临时合成数据和不可连接的下载器配置；尚不覆盖正式 App 导航全流程、真人操作、真实 PT/客户端服务和 ARM64 正式发行或跨版本升级。

**正式 App 导航及执行记录中的浏览器审批门禁已通过（GitHub CI run `35463902355`）**：测试以正式 `App.vue` 和真实 FastAPI 的任务定义、执行记录、底层任务为入口，自动从任务中心导航至“审核 / 对账”，完成合成任务分析、管理员会话审批及 Transmission 无副作用执行计划；验证计划与底层任务绑定且没有 Hardlink、journal 或源文件变化。并不执行真实下载器写操作，不覆盖真实 PT、真人点击或正式 ARM64 跨版本升级与发布验收。

**v1.0.0 多平台发布资产一致性门禁已通过离线回归（GitHub CI run `35465172086`）**：发布流水线在上传资产前验证不可变 GHCR index digest 与清单、双架构声明、提交/tag、SPDX 文件哈希及 SHA256SUMS 一致，并拒绝多余或不安全文件。相关单元测试使用离线合成资产，不是正式 v1.0.0 镜像发布或 ARM64 版本升级的真实环境证据。

**ARM64 合成双镜像备份/恢复门禁已通过（GitHub CI run `35470043525`）**：原生 ARM64 Runner 使用不同的本地同版本 ARM64 image ID 演练完整数据库探针/备份、候选接管与原基线备份恢复、旧容器再次 readiness；与现有 updater 合成替换/故障回滚独立执行。无上一正式 ARM64 版本，因此不能将该门禁描述为正式 ARM64 跨版本升级或多平台 GHCR 发布验收；正式 AMD64 v0.1.9 基线与原有正式跨版本门禁不变。

**GHCR 多平台子镜像清单离线回归门禁已通过（GitHub CI run `35476485411`）**：发布工作流对每个宣称的平台读取不可变 child manifest，核对真实子镜像配置与层描述符存在且结构有效，拒绝无效或重复 digest 等异常；离线测试尚不等于正式 v1.0.0 GHCR 发布后的真实性验证，子 config 实际 CPU 架构、版本标签和原生运行仍需独立检查。

**发布子镜像实际 CPU 架构与版本身份门禁已通过离线回归、旧版真实 GHCR 只读验证（GitHub CI run `35477811583`、Candidate Docker E2E run `35477811590`）**：发布工作流在上传资产前将独立 tag/commit 与每个平台不可变子镜像的 `.Image` config 比对，要求实际架构、rootfs 与版本/修订标签正确；Candidate Docker E2E 已只读检查正式 v0.1.9 的真实 AMD64 不可变 baseline。此两项测试不是 v1.0.0 正式双架构 GHCR 配置拉取或两架构正式镜像运行证据。

**不可变 GHCR 镜像隔离运行门禁（CI run `35490479646`、Candidate Docker E2E `35490479645` 通过；v1.0.0 正式 digest 待验收）**：Candidate Docker E2E run `35480067748` 已对正式 v0.1.9 AMD64 基线不可变 digest 完成隔离启动、版本/架构身份、数据库预检与备份；原生 ARM64 CI run `35480586206` 已通过本地候选运行与下载器链路。正式 Release workflow 改为：镜像 index 推送后完成 AMD64 原生和 QEMU ARM64 运行，再由另一原生 ARM64 Job 拉取**相同不可变 index digest** 运行；最后一个资产发布 Job 必须等待这两阶段通过并重新核对版本 tag digest。原生 ARM64 的本地候选或 QEMU 测试均不能替代正式 v1.0.0 GHCR 不可变 digest 的原生运行验收；该版本尚未发布。

**GitHub Release 上传后回读一致性门禁（提交 `0c31045`，CI run `35501276565` 离线回归通过）**：Release workflow 在上传后读回正式 Release 元数据及实际下载的三份资产，再与独立 tag/commit/不可变镜像 digest、SBOM 和 SHA256SUMS 交叉核验；失败不会推进 stable/latest。移动通道前再次确认 GitHub 最新正式 tag 仍为当前版本。新增单元测试以模拟 CLI/合成资产验证失败关闭，不等于正式 v1.0.0 资产已发布或真实双架构运行。

当前开发 Runner 没有 Docker daemon，真实容器验收由 GitHub Actions 独立 Runner 承担。当前最新正式 Release 为 [v1.0.0](https://github.com/YYxiaoma/PackBreaker/releases/tag/v1.0.0)，公开 GHCR 不可变镜像为 `ghcr.io/yyxiaoma/packbreaker@sha256:fce1f3e3c0f56a8c024bbbf46a65fbc4ca51225a5c2bc2543fdddbb02c21a0c4`。它的双架构运行、AMD64 相邻升级/回滚与发布资产回读证据详见 [恢复发布 run `35507181886`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35507181886)；首次失败的 Release run 保留原始失败记录。

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

候选 v1.0.1 Profile Registry 另列出以下**待正式放行**类型，但它们当前不能创建正式配置、保存凭证或执行临时 probe：KeepFrds（`https://pt.keepfrds.com`）、HDHome（`https://hdhome.org`）、UBits（`https://ubits.club`）、HDFans（`https://hdfans.org`）、BTSCHOOL（`https://pt.btschool.club`）、PTTime（`https://www.pttime.org`）、Rousi Pro（`https://rousi.pro`）和聆音Club（`https://pt.soulvoice.club`）。前六项及聆音Club按 NexusPHP/Cookie profile 建模，各有**一个**真实 Torrent 读取与元信息解析成功样本；这不是全部种子类型或真实任务验收。Rousi Pro 以 API Key 搜索、独立 Cookie 取种，已有固定同源真实只读认证、搜索及**一次 Cookie-only 取种**的有效 v1 单文件元信息证据，且有合成的双凭据/分析至模拟 Transmission 添加、强制校验、做种与释放链路；独立 `fetch_details()` 仍未验收，完整真实任务与受控下载器现场证据仍不足。其 Cookie 不得冒充 API Key，API Key 也不具有已证实的取种权限。所有这些 profile 的 `support_status` 均为 `PENDING_ADAPTER`，数据库能容纳新站点类型不代表允许启用；只有逐站满足协议、错误归类、权限安全和必要真实任务验收后才能扩大上表的正式支持集合。详见 [v1.0.1 研发记录](./v1.0.1-development.md)。

**v1.0.2 研发候选（尚未正式发布）**：上述八个站点改为 `PENDING_REAL_VALIDATION`，可以在添加站点时选择、加密保存对应凭据并运行固定同源的只读连接测试；删除添加/编辑表单的站点地址输入框。它们仍不可启用生产辅种任务，服务层正式任务白名单只包含原有 M-TEAM、HDTime、HHClub。此开发分支的配置能力不适用于已发布的 v1.0.1 Docker 镜像；详见 [v1.0.2 研发记录](./v1.0.2-development.md)。八站已有的有限只读取种或合成链路证据不变，不能据此宣称通过真实完整辅种验收。

站点凭证只写入加密 secret store，管理 API/UI 不回显已保存明文。站点临时故障、鉴权失败和限流不会成为放宽 torrent 内容验证的理由。

2026-09-23 七个 NexusPHP 候选站点的一次两页只读搜索抽样中，KeepFrds、HDHome、UBits、BTSCHOOL、PTTime、聆音Club 均取得两页候选、四项字段可解析且跨页没有重复；请求的 `page_size=20` 实际返回 50 或 100 条。HDFans 在第一页返回 `SITE_UNAVAILABLE`，本轮未完成跨页验收，未自动重试。仅此抽样不证明全部分页稳定性，也不能代替受控真实辅种与回滚；正式放行范围保持不变。 2026-09-23 单次独立复核中 HDFans 再次于第一页返回 `SITE_UNAVAILABLE`，仍无跨页证据；未追加请求或推断原因。新增只读验收工具将欠缺大小/日期/做种及下载数的两页结果标记为不完整，并针对后续失败仅提供脱敏分类，不改变正式支持门禁。

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
- `release-baseline.json` 当前固定正式 `v1.0.0` 双架构 digest；后续候选版本必须以该最新 published baseline 做相邻版本升级/回滚门禁后才允许进入正式发布。若目标 ARM64 上不存在对应旧版镜像，不能虚构历史跨架构升级路径。
- `v0.1.2` 正式提供独立 updater helper 的 Web 一键升级链路；`v0.1.7` 起正式支持显式挂载 docker.sock 的 Compose 单容器使用同一 Web 升级链，并保留 Compose labels。自动容器替换仍只承诺单个 PackBreaker 容器、唯一可写 `/config`、官方 GHCR 镜像、可安全重建的端口/环境/挂载/restart policy 和单网络配置。复杂 namespace、多网络、显式静态 IP/MAC 或 AutoRemove 容器继续失败关闭。

## 6. 兼容承诺原则

支持矩阵只会在自动化协议证据与必要的真实环境证据同时满足后扩大。未验证的新下载器版本、站点实现、CPU 架构或文件系统不会被静默视为兼容；无法证明时一律保持人工确认、阻断或只读诊断。
