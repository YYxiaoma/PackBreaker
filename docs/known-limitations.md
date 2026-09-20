# PackBreaker v1.0 已知限制

本页记录当前实现中有意保留的边界和尚未取得的发布证据。它们不是绕过安全门的理由；相反，PackBreaker 在这些条件下应保持人工确认、只读、阻断或显式手工运维。

## 1. 自动匹配与真实样本

- 自动候选批准继续关闭。当前真实标定只有高置信正确样本，没有足够的高置信内容错误负样本，因此 `recommended_threshold=null`；不能从现有数据估计可接受误报率。
- 99% repair 代码链已完成，但真实环境截至 2026-09-14 仍没有自然产生的 `CLIENT_CHECK_REQUIRED/RETRY` 可用于 field E2E。不会通过破坏真实媒体或伪造 RETRY 来制造验收样本。
- 新真实故障继续归档，但 RF-001～RF-007 已满足“7 个失败样例”数量要求，不再为了数量主动制造失败。

## 2. 发布与容器证据

- v1.0.0 正式镜像支持 `linux/amd64` 和 `linux/arm64`（aarch64）；ARMv7 和其他平台不在正式支持范围内。ARM64 正式摘要已通过原生和隔离 QEMU 运行、预检与备份；用户真实 PT/生产下载器环境仍需单独现场验收。
- 当前开发 Runner 无 Docker daemon；GitHub Actions 持续承担容器、备份/恢复和 updater 的真实 Docker 门禁。Release workflow run `35429394091` 已为 `v0.1.9` 提供真实 Docker 构建、`v0.1.8 → v0.1.9 → v0.1.8` 升级/回滚与 updater helper E2E 证据，并继续覆盖既有 Compose labels 与 docker.sock 保留要求。
- 当前最新正式 Release 为 [v1.0.0](https://github.com/YYxiaoma/PackBreaker/releases/tag/v1.0.0)，正式不可变镜像为 `ghcr.io/yyxiaoma/packbreaker@sha256:fce1f3e3c0f56a8c024bbbf46a65fbc4ca51225a5c2bc2543fdddbb02c21a0c4`。上一正式 v0.1.9 的 AMD64-only 镜像继续保留其原 digest。
- [正式恢复发布 run `35507181886`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35507181886) 已用原已推送镜像在独立 AMD64、QEMU ARM64 与原生 ARM64 Runner 完成运行验证，AMD64 v0.1.9→v1.0.0→v0.1.9 正式相邻升级/回滚通过，Release 资产上传回读和 `stable/latest` 与原不可变摘要一致。后续版本的 `release-baseline.json` 已推进到 v1.0.0 双架构镜像。
- **历史失败记录（已恢复）**：2026-09-20 的首次 [Release run `35504683200`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35504683200) 在 QEMU ARM64 运行检查处失败，原生 ARM64 摘要运行及 GitHub Release 资产发布当时被跳过。后续恢复单独完成全部门禁，没有事后更改该失败记录，亦未覆盖已推送的版本镜像。首次失败根因仍无完整原始错误日志，不能断言已确诊；详见 [发布流程与恢复记录](./release-process.md)。
- [独立诊断 run `35506113046`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35506113046) 已在原生 ARM64 与 QEMU 上使用相同正式 index digest 通过完整启动/备份门禁，首次 QEMU 失败未稳定复现，仍无权推断具体根因。独立诊断只是恢复发布的前置证据，不应与正式发布资产成功混淆。
- [首次恢复 run `35506579423`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35506579423) 的同一 AMD64 Runner 在完成 AMD64 摘要运行及正式相邻升级回滚后，ARM64 QEMU 脚本仍失败；独立原生 ARM64 通过，发布资产继续被阻断。后续将 QEMU 与 AMD64 拆分到不同 Runner 后重新复验，不得根据独立诊断单次成功跳过 QEMU 门禁。

## 3. 升级与 Docker 权限

- 独立 `docker run --name packbreaker` 支持“单常驻容器”一键升级：主容器挂载 docker.sock，点击版本弹窗的升级按钮后临时创建 `AutoRemove` helper 接管停机、容器替换、健康检查与失败回滚；升级窗口内会短暂存在第二个 helper 容器，结束后自动删除。若不愿向主容器授予 docker.sock，仍可额外常驻独立 `packbreaker-updater` 作为兼容的最小权限方案。
- 自动升级只支持能够安全重建的单容器部署：必须存在唯一可写 `/config` 挂载，当前镜像必须来自官方 GHCR；Compose 管理容器在显式挂载 docker.sock 时可使用 Web 一键升级，并保留 Compose labels，但 updater 不修改宿主机 `compose.yaml`，因此升级后需同步 YAML 的 image digest。`AutoRemove`、`container:<id>` network/PID/IPC namespace、多网络、显式静态 IP/MAC 等配置仍会阻断自动升级；Swarm/Kubernetes 拓扑仍需宿主机管理员按 runbook 手工升级。
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
