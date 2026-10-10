# PackBreaker 已知限制（更新于 2026-10-10）

**正式版本仍为 v1.1.1；v1.1.2 已完成最新候选镜像交付，但真实站点与下载器业务验收尚未关闭。** 源码 `e784c19` 的 GHCR / Docker Hub 同摘要双架构候选已通过 AMD64、原生 ARM64 和隔离升级/回滚（[Actions #38025511243](https://github.com/YYxiaoma/PackBreaker/actions/runs/38025511243)），不能将其视为正式 Release 或真实辅种成功。v1.1.2 不要求 Synology NAS 现场验收，不得因此擅自操作生产环境。完整清单见 [Issue #5](https://github.com/YYxiaoma/PackBreaker/issues/5)。

## 1. 尚未完成的站点与内容校验

- PTTime：搜索有六条结果，其中一条大小相符、详情身份匹配；详情页仍不能提供独立的可靠容量证据。尚未取得当前**新候选**的 `.torrent` 元信息及独立 Piece Hash。
- 2026-10-10 新一轮 PTTime 只读关键词搜索返回 `CANDIDATE_FOUND`，没有 Torrent 下载或下载器写入，也不代表该搜索候选已与现有媒体完成内容绑定。当前独立工作区仅可直接访问历史站点验收凭据文件，未恢复新增七站原审核加密配置；Audiences、HDDolby、SpringSunday 的本轮 `CONFIG_BLOCKED` 是**检查前置配置缺失**，不得记录为新的站点网络故障。
- HDFans：既有 Transmission 任务报告 7,321 个 Piece 均可用，但只是客户端对**旧种子**的校验，不能冒充当前新候选的独立验证。
- HDHome、UBits、KeepFrds、BTSCHOOL、PTTime、聆音Club 还需逐站完成正式 PackBreaker 任务全链路验收。Tracker 现有种子在做种不代表新任务的操作流水或受控恢复已经通过。
- 七新增站点 PTerClub、Audiences、SpringSunday、HDDolby、U2、不可躺、CarPT 全部保持 `PENDING_REAL_VALIDATION`，不能打开正式辅种；详见 [支持矩阵](./support-matrix.md)。
- 如果真实站点内容获取被执行环境拦截，不能换工具、改变请求形态或通过浏览器绕过。需要保留阻断并用符合站点权限的合法验证路径继续。

## 2. 站点网络、Tracker 与错误处理

- PTerClub、SpringSunday、U2、HDFans 存在间歇性连接、搜索失败或 `SITE_UNAVAILABLE` 记录；域名可以解析不证明站点稳定，不能将超时误判为 Cookie 永久失效。
- 2026-10-09 单任务只读记录显示 UBits 最近一次 announce 成功、BTSCHOOL 两条一成一超时、Rousi Pro 一条未成功；2026-10-10 **更大范围** Transmission 只读快照返回 6,744 条客户端任务，其 Tracker 状态条目 BTSCHOOL 705 成功/3 失败（2 超时）、Rousi Pro 0 成功/3 失败、UBits 379 成功/81 失败（64 超时）。两次记录统计口径不同，不可相加；**不是**独立种子数量或 Tracker 故障根因。所有敏感 passkey、原始 announce URL 与凭证不得输出。
- v1.1.2 主线已合并匹配错误/超时重试分类、限流 `Retry-After` 秒数与 HTTP-date 安全等待、Rousi/NexusPHP 临时故障受限恢复和浏览器 E2E 竞态修复（PR #11–#14）。相关自动化测试及合并后主线 CI 已通过，但**真实网络维护、下载器短暂离线、辅助文件已补齐、人工干预等受控恢复场景**仍需现场逐项验证。
- 站点限流、身份验证、JS Challenge 和验证码不能靠自动浏览器或伪造来源跳过。具有一次性语义的 torrent 下载 token 不允许盲目重试。

## 3. 媒体、下载器和恢复安全

- 源影片默认只读，不截断、不覆盖、不删除。仅在经过明确授权的挂载根内可操作目标文件；跨设备硬链接、符号链接越界、路径穿越一律失败关闭。
- qBittorrent / Transmission 的 ADD/VERIFY/START/REMOVE 必须有 journal 及操作前后身份、路径、哈希与 ownership 证据。已存在外部同 hash 种子不能推定归属；丢失响应或凭证不能靠重发写操作“碰运气”。
- `FULL_VERIFIED` 和对应的 qBittorrent API capability 才允许相应 skip-checking 路径；Transmission 不因独立 Piece 校验而跳过客户端下载器 verify。未来 WebAPI 版本行为变化须重新验收。
- 自动修复隔离和受控清理要求受支持的文件系统 xattr；不支持时转为人工对账或安全阻断，不允许删除身份不明的文件。
- 单容器 Docker Web 在线升级需要具有足够权限的 `docker.sock`，并仅支持可安全重建的容器拓扑。备份与旧镜像可用性不满足时必须进入 `manual_recovery_required`，不能保证任何 NAS 环境都能自动回滚。

## 4. 发布与验收的界线

- v1.1.1 正式双架构身份及 v1.1.2 当前源码 `e784c194a4af19f21b6fbce4ec4215f6b99fa444` 已经过四项精确 SHA CI/Docker E2E、双注册表同一不可变索引摘要 `sha256:b6289c9ebf311afe9a1ce9f49eb1eefb854d4885b7177b3d4065416a7cfcdc6c` 与原生 ARM64 运行验收；**v1.1.2 依然不是正式 Release，也未移动 latest/stable**。旧 `24f0213` 镜像不是当前候选。
- 已完成候选源码 `e784c19` 的 CI、相邻升级/回滚与双架构镜像交付，但**后续任何代码改动**均需重新生成精确 SHA 候选并重做相关门禁。v1.1.2 正式发布前仍需真实站点 / 内容 / 下载器受控业务验收及用户单独授权；未解决的外部阻断需可审计记录和获批准的范围决策。
- **Synology NAS 现场验收不在 v1.1.2 发布门槛中**；此决定不授予 NAS 生产读写、升级、回滚权限。
- `latest`、`stable` 属于可变发布发现渠道，不可代替不可变 digest；只有用户正式发布授权后才推进。完整来源： [收官 Issue #5](https://github.com/YYxiaoma/PackBreaker/issues/5)、[v1.1.2 研发记录](./v1.1.2-development.md)。

## 5. 持续保留的产品与运维限制

- **自动批准阈值**：已有高置信正确样本不足以估计误报率；在取得足够的高置信错误负样本并完成标定之前，`recommended_threshold=null`，不得把片名、大小、评分或采样 Hash 当成自动批准的完整内容证据。
- **99% repair 现场样本**：修复、inode 隔离和操作流水代码已有自动化证据，但仍缺自然产生的 `CLIENT_CHECK_REQUIRED/RETRY` 真实现场样本；不得破坏源媒体或制造错误来凑齐验收。
- **备份密钥与恢复入口**：SQLite 数据库一致性备份有意不包含 `secret.key`；脱离对应密钥无法解密已保存凭证。数据库离线恢复须停止活动实例并使用维护 CLI，当前不提供直接替换在线数据库的 Web 按钮。
- **运维 API 范围**：管理员会话是当前 Web 管理 API 的认证边界；通用 Bearer/API Token 自动化访问、下载完成 Webhook 的 HMAC/防重放接口仍未正式开放。operation journal 仅支持满足终态、保留期和引用检查的逐条安全 purge，不支持跳过这些检查的批量清理。
- **日志与诊断**：安全诊断 ZIP 默认不包含运行日志；需要独立使用具有权限和脱敏保护的日志查询/导出入口。遇到无法证明的文件归属、Piece、路径或客户端状态时保持阻断并留下可审计记录。
