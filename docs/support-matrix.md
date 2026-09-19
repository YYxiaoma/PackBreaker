# PackBreaker v1.0 支持矩阵

本页定义当前明确支持或已经现场验证的运行边界。未列为“支持”的组合不等于一定不可用，但在取得对应协议、恢复和真实环境证据前不得作为正式兼容承诺。

## 1. 发布平台

| 项目 | 当前状态 | 说明 |
| --- | --- | --- |
| Linux `amd64` 容器 | 当前已发布目标 | v0.1.9 正式镜像仅包含 `linux/amd64`；正式发布身份必须使用完整 image digest。 |
| Linux 其他架构 | 未声明支持 | 尚无构建、恢复和性能验收矩阵。 |
| Windows / macOS 原生生产运行 | 未声明支持 | 可用于开发，但 v1.0 生产部署以 Linux 容器为边界。 |

**v1.0.0 研发中（尚未正式支持）**：源码已增加 `linux/arm64`（aarch64）双架构发布契约、CI 原生 ARM64 构建/测试任务、发布 manifest 平台验证及 Web 升级跨架构阻断；目前仅完成 AMD64 研发 Runner 的自动化验收，尚缺原生 ARM64 Docker、核心业务链、真实发布镜像及升级/回滚现场证据。取得上述证据并发布 v1.0.0 后才能调整正式支持声明。ARMv7 不包含在本次目标中，详细退出条件见 [研发路线图](./development-roadmap.md)。

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
