# PackBreaker v1.0 已知限制

本页记录当前实现中有意保留的边界和尚未取得的发布证据。它们不是绕过安全门的理由；相反，PackBreaker 在这些条件下应保持人工确认、只读、阻断或显式手工运维。

## 1. 自动匹配与真实样本

- 自动候选批准继续关闭。当前真实标定只有高置信正确样本，没有足够的高置信内容错误负样本，因此 `recommended_threshold=null`；不能从现有数据估计可接受误报率。
- 99% repair 代码链已完成，但真实环境截至 2026-09-14 仍没有自然产生的 `CLIENT_CHECK_REQUIRED/RETRY` 可用于 field E2E。不会通过破坏真实媒体或伪造 RETRY 来制造验收样本。
- 新真实故障继续归档，但 RF-001～RF-007 已满足“7 个失败样例”数量要求，不再为了数量主动制造失败。

## 2. 发布与容器证据

- 正式发布目标当前只有 `linux/amd64`。
- 当前开发 Runner 无 Docker daemon；GitHub Actions 持续承担容器、备份/恢复和 updater 的真实 Docker 门禁。Release workflow run `35297829243` 已为 `v0.1.6` 提供真实 Docker 构建、相邻版本升级/回滚与 updater helper E2E 证据。
- 当前最新正式 Release 为 `v0.1.6`，正式不可变镜像为 `ghcr.io/yyxiaoma/packbreaker@sha256:b250b4dd945648fca884989d4c6ce14692dea839d4364f462d6080806357f13d`。
- 已发布版本继续由跨版本 Docker 升级/恢复门禁与 updater helper E2E 提供正式证据；当前 `release-baseline.json` 已推进到正式 `v0.1.6`，作为下一候选版本的相邻兼容输入。

## 3. 升级与 Docker 权限

- 独立 `docker run --name packbreaker` 支持“单常驻容器”一键升级：主容器挂载 docker.sock，点击版本弹窗的升级按钮后临时创建 `AutoRemove` helper 接管停机、容器替换、健康检查与失败回滚；升级窗口内会短暂存在第二个 helper 容器，结束后自动删除。若不愿向主容器授予 docker.sock，仍可额外常驻独立 `packbreaker-updater` 作为兼容的最小权限方案。
- 自动升级只支持能够安全重建的单容器部署：必须存在唯一可写 `/config` 挂载，当前镜像必须来自官方 GHCR；Docker Compose 管理标签、`AutoRemove`、`container:<id>` network/PID/IPC namespace、多网络、显式静态 IP/MAC 等配置会阻断自动升级。Compose/Swarm/Kubernetes 拓扑仍需宿主机管理员按 runbook 手工升级。
- `/var/run/docker.sock` 等价于 Docker 主机级管理权限；单容器易用模式把该权限授予主 PackBreaker，因此只应在受信宿主机启用。服务端升级入口仍限制到官方 Release 不可变 digest 和目标 `packbreaker` 容器，但这不能把 Docker socket 本身变成低权限接口。
- 自动回滚依赖旧镜像仍可启动且 `/config` 可写；若 Docker daemon、卷、旧镜像或离线恢复本身不可用，helper 会进入 `manual_recovery_required`，不会继续覆盖现场。
- `stable` 是可移动发现通道，不能作为生产安装、升级或回滚的唯一身份；运维记录必须保存完整 image digest。

## 4. 下载器与站点版本范围

- 已真实验证的下载器基线是 qBittorrent 5.2.3 / WebAPI 2.15.1 与 Transmission 4.1.3。未来版本必须重新做能力和真实链路验收。
- qBittorrent WebAPI 2.16.0 已移除 `skip_checking`；当前 2.15.1 的 FULL_VERIFIED skip-check 优化不能直接外推到 2.16+。
- HHClub 当前只接受 `https://hhanclub.net`。旧域名、镜像域名或未验证 origin 不自动信任。

- 2026-09-18 的 v0.1.6 现场只读验收中，HDTime 主站经 Cloudflare 返回 HTTP 500；现有 Cookie 与浏览器仿真均得到 SITE_UNAVAILABLE 而非鉴权失败。站点恢复前保留已有适配器支持声明，但不把本轮现场状态记为通过，也不自动改用镜像域名。
- v0.1.6 AI Provider、Telegram AI 与新增 PT 站点仍缺本轮真实外部凭证/目标证据；自动化与 MockTransport 通过不能替代真实 Provider/Chat/站点验收。详见 [v0.1.6 真实环境验收记录](./v0.1.6-real-environment-acceptance.md)。

## 5. 运维功能边界

- operation journal retention 只提供逐条安全 purge，不提供批量 purge；每条都必须重新证明终态、保留期和引用关系。
- repair inode isolation / 自动 cleanup 依赖目标文件系统支持 Linux `user.*` xattr。若 NAS/网络文件系统未提供该能力，PackBreaker 会在 isolation 阶段阻断或在 cleanup 阶段转入人工对账，不会退回到仅依赖 inode 的自动删除。
- 数据库在线恢复没有 Web 按钮。恢复要求停止活动 PackBreaker 实例并使用维护 CLI，以避免运行进程持有旧连接时替换数据库。
- 一致性数据库备份有意不包含 `secret.key`。只恢复数据库而没有对应主密钥时，已加密凭证不可解密。
- 安全诊断 ZIP 默认不包含运行日志；日志查询/导出是独立、受限并再次脱敏的接口。
- 下载完成 Webhook 的 HMAC/防重放仍是计划契约，当前没有开放真实接口；API Token/Bearer 自动化访问也已移除，因此当前管理 API 仅支持管理员会话。

## 6. 安全默认值

当版本、路径、ownership、当前 torrent 状态、piece 内容、operation journal 或恢复证据无法被严格证明时，PackBreaker 的预期行为是失败关闭或要求人工处理，而不是猜测成功。任何后续“易用性优化”都不能把这些限制变成隐式放宽。
