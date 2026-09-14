# PackBreaker v1.0 支持矩阵

本页定义当前明确支持或已经现场验证的运行边界。未列为“支持”的组合不等于一定不可用，但在取得对应协议、恢复和真实环境证据前不得作为正式兼容承诺。

## 1. 发布平台

| 项目 | 当前状态 | 说明 |
| --- | --- | --- |
| Linux `amd64` 容器 | 发布目标 | Dockerfile 与 release workflow 只声明 `linux/amd64`；正式发布身份必须使用完整 image digest。 |
| Linux 其他架构 | 未声明支持 | 尚无构建、恢复和性能验收矩阵。 |
| Windows / macOS 原生生产运行 | 未声明支持 | 可用于开发，但 v1.0 生产部署以 Linux 容器为边界。 |

当前开发 Runner 没有 Docker daemon，但 GitHub Actions CI run `34851129257` 已在 `ubuntu-latest` Docker 环境实际完成镜像构建、空配置启动/readiness、preflight、一致性备份、停服务离线 verify/restore 与重启 readiness。正式 release-to-release 的跨镜像升级/回滚仍需在首个正式 tag 之后继续积累证据。

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

站点凭证只写入加密 secret store，管理 API/UI 不回显已保存明文。站点临时故障、鉴权失败和限流不会成为放宽 torrent 内容验证的理由。

## 4. 数据与文件系统

- SQLite 是 v1.0 唯一数据库后端；Runtime 启动使用 Alembic head 校验和安全临时副本升级。
- `/config` 必须是可写真实目录并满足最小权限要求；`secret.key` 需要独立安全保管，不包含在数据库备份中。
- `/data` 作为媒体只读根；业务链不得修改源文件内容或替换源 inode。
- 零复制辅种依赖源与目标位于支持 hardlink 的同一设备；跨设备或证据不确定场景必须失败关闭或进入明确的人工/下载器校验流程。
- 受控 repair inode isolation 与自动 cleanup 额外要求目标文件系统支持 Linux `user.*` extended attributes。PackBreaker 会在独立 repair target 上持久化 `user.packbreaker.repair_owner` ownership marker；文件系统不支持 xattr、marker 缺失或 marker 与 isolation journal 不一致时必须失败关闭，不能仅凭 inode/size 推断所有权。普通 hardlink 辅种不依赖该 marker。
- 路径映射采用明确 remote/container 前缀，禁止路径穿越、符号链接逃逸和未证明目标目录。

## 5. 升级与回滚

- 当前数据库 head 为 `0023_backup_policy`；自动化矩阵覆盖 `0001`～`0022` 到当前 head。
- 生产升级使用不可变 `<image>@sha256:<digest>`；`stable` 只用于发现，不是部署身份。
- 数据库升级先创建 `pre-upgrade` 一致性快照，在同文件系统临时副本完成迁移与验证后再原子切换。
- 生产回滚不依赖 Alembic 原地 downgrade；旧镜像不能读取新 schema 时必须恢复兼容的升级前/离线备份。
- 首个正式 release 之前不存在“上一正式 release 镜像 → 当前 release 镜像”的跨镜像升级矩阵；该证据只能在首个正式 tag 后开始积累。

## 6. 兼容承诺原则

支持矩阵只会在自动化协议证据与必要的真实环境证据同时满足后扩大。未验证的新下载器版本、站点实现、CPU 架构或文件系统不会被静默视为兼容；无法证明时一律保持人工确认、阻断或只读诊断。
