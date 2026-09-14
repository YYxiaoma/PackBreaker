# PackBreaker v1.0 已知限制

本页记录当前实现中有意保留的边界和尚未取得的发布证据。它们不是绕过安全门的理由；相反，PackBreaker 在这些条件下应保持人工确认、只读、阻断或显式手工运维。

## 1. 自动匹配与真实样本

- 自动候选批准继续关闭。当前真实标定只有高置信正确样本，没有足够的高置信内容错误负样本，因此 `recommended_threshold=null`；不能从现有数据估计可接受误报率。
- 99% repair 代码链已完成，但真实环境截至 2026-09-14 仍没有自然产生的 `CLIENT_CHECK_REQUIRED/RETRY` 可用于 field E2E。不会通过破坏真实媒体或伪造 RETRY 来制造验收样本。
- 新真实故障继续归档，但 RF-001～RF-007 已满足“7 个失败样例”数量要求，不再为了数量主动制造失败。

## 2. 发布与容器证据

- 正式发布目标当前只有 `linux/amd64`。
- 当前开发 Runner 无 Docker daemon，因此 CI 中定义的空配置启动、离线 restore、重启 readiness 和后续目标升级/回滚流程尚缺本地实跑证据；必须在 GitHub/目标 Docker 环境补齐。
- 尚未创建首个正式 `v<version>` tag，所以真实 GHCR image digest、SPDX SBOM、`SHA256SUMS` 和 GitHub Release 资产还没有发布级实证。
- 首个正式 release 之前没有上一正式镜像可用于跨镜像升级/回滚矩阵。当前 Alembic 历史 revision 矩阵只能证明数据库 schema 路径和原子回滚机制。

## 3. 升级与 Docker 权限

- Web “升级中心”不会直接操作 Docker，也不会因为检测到 docker.sock 就获得容器写能力；它只提供当前版本、本地 release preflight、升级前备份和不可变 digest 手工 runbook。
- 容器拉取、替换、失败回滚和离线数据库恢复仍由宿主机管理员显式执行。默认部署不要求挂载 docker.sock。
- `stable` 是可移动发现通道，不能作为生产安装、升级或回滚的唯一身份；运维记录必须保存完整 image digest。

## 4. 下载器与站点版本范围

- 已真实验证的下载器基线是 qBittorrent 5.2.3 / WebAPI 2.15.1 与 Transmission 4.1.3。未来版本必须重新做能力和真实链路验收。
- qBittorrent WebAPI 2.16.0 已移除 `skip_checking`；当前 2.15.1 的 FULL_VERIFIED skip-check 优化不能直接外推到 2.16+。
- HHClub 当前只接受 `https://hhanclub.net`。旧域名、镜像域名或未验证 origin 不自动信任。

## 5. 运维功能边界

- operation journal retention 只提供逐条安全 purge，不提供批量 purge；每条都必须重新证明终态、保留期和引用关系。
- repair inode isolation / 自动 cleanup 依赖目标文件系统支持 Linux `user.*` xattr。若 NAS/网络文件系统未提供该能力，PackBreaker 会在 isolation 阶段阻断或在 cleanup 阶段转入人工对账，不会退回到仅依赖 inode 的自动删除。
- 数据库在线恢复没有 Web 按钮。恢复要求停止活动 PackBreaker 实例并使用维护 CLI，以避免运行进程持有旧连接时替换数据库。
- 一致性数据库备份有意不包含 `secret.key`。只恢复数据库而没有对应主密钥时，已加密凭证不可解密。
- 安全诊断 ZIP 默认不包含运行日志；日志查询/导出是独立、受限并再次脱敏的接口。
- 下载完成 Webhook 的 HMAC/防重放仍是计划契约，当前原型没有开放真实接口；自动化接入应使用已经实现并受 scope 约束的 API Token/公开任务接口。

## 6. 安全默认值

当版本、路径、ownership、当前 torrent 状态、piece 内容、operation journal 或恢复证据无法被严格证明时，PackBreaker 的预期行为是失败关闭或要求人工处理，而不是猜测成功。任何后续“易用性优化”都不能把这些限制变成隐式放宽。
