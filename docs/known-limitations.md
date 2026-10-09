# PackBreaker 已知限制（更新于 2026-10-09）

**正式版本 v1.1.1，v1.1.2 处于遗留问题收官研发阶段。** 阶段性候选镜像、自动化回归和既有下载器做种状态均不能自动关闭真实站点验收。v1.1.2 不要求 Synology NAS 现场验收，不得因此擅自操作生产环境。完整清单与事实更新见 [Issue #5](https://github.com/YYxiaoma/PackBreaker/issues/5)。

## 1. 尚未完成的站点与内容校验

- PTTime：搜索有六条结果，其中一条大小相符、详情身份匹配；详情页仍不能提供独立的可靠容量证据。尚未取得当前**新候选**的 `.torrent` 元信息及独立 Piece Hash。
- HDFans：既有 Transmission 任务报告 7,321 个 Piece 均可用，但只是客户端对**旧种子**的校验，不能冒充当前新候选的独立验证。
- HDHome、UBits、KeepFrds、BTSCHOOL、PTTime、聆音Club 还需逐站完成正式 PackBreaker 任务全链路验收。Tracker 现有种子在做种不代表新任务的操作流水或受控恢复已经通过。
- 七新增站点 PTerClub、Audiences、SpringSunday、HDDolby、U2、不可躺、CarPT 全部保持 `PENDING_REAL_VALIDATION`，不能打开正式辅种；详见 [支持矩阵](./support-matrix.md)。
- 如果真实站点内容获取被执行环境拦截，不能换工具、改变请求形态或通过浏览器绕过。需要保留阻断并用符合站点权限的合法验证路径继续。

## 2. 站点网络、Tracker 与错误处理

- PTerClub、SpringSunday、U2、HDFans 存在间歇性连接、搜索失败或 `SITE_UNAVAILABLE` 记录；域名可以解析不证明站点稳定，不能将超时误判为 Cookie 永久失效。
- 2026-10-09 真实 Transmission 只读 RPC 复查：UBits 的最近一次 announce 已报告成功；BTSCHOOL 有一条成功、一条超时；Rousi Pro 一条仍未报告成功（未标记超时）。这些是**客户端状态**，不说明 Tracker 根因，不输出 passkey、原始 announce URL 或凭证。
- 自动匹配对错误/超时具备受限重试及人工介入流程，但在网络维护、下载器短暂离线或辅助文件已补齐情况下的真实受控恢复仍需单独核对。
- 站点限流、身份验证、JS Challenge 和验证码不能靠自动浏览器或伪造来源跳过。具有一次性语义的 torrent 下载 token 不允许盲目重试。

## 3. 媒体、下载器和恢复安全

- 源影片默认只读，不截断、不覆盖、不删除。仅在经过明确授权的挂载根内可操作目标文件；跨设备硬链接、符号链接越界、路径穿越一律失败关闭。
- qBittorrent / Transmission 的 ADD/VERIFY/START/REMOVE 必须有 journal 及操作前后身份、路径、哈希与 ownership 证据。已存在外部同 hash 种子不能推定归属；丢失响应或凭证不能靠重发写操作“碰运气”。
- `FULL_VERIFIED` 和对应的 qBittorrent API capability 才允许相应 skip-checking 路径；Transmission 不因独立 Piece 校验而跳过客户端下载器 verify。未来 WebAPI 版本行为变化须重新验收。
- 自动修复隔离和受控清理要求受支持的文件系统 xattr；不支持时转为人工对账或安全阻断，不允许删除身份不明的文件。
- 单容器 Docker Web 在线升级需要具有足够权限的 `docker.sock`，并仅支持可安全重建的容器拓扑。备份与旧镜像可用性不满足时必须进入 `manual_recovery_required`，不能保证任何 NAS 环境都能自动回滚。

## 4. 发布与验收的界线

- 已通过 v1.1.1 正式 AMD64/ARM64 发布身份，以及 v1.1.2 阶段性候选的两注册表摘要与原生 ARM64 运行检查。**v1.1.2 尚非正式 Release。**
- v1.1.2 正式发布需要完成真实站点 / 内容 / 下载器受控验收、修复确认、适配状态及文档对账，再对**最终源码 SHA**重做 CI、相邻升级/回滚、不可变双架构候选验证。任何未关闭的外部阻断需明确记录和获批准的范围决策。
- **Synology NAS 现场验收不在 v1.1.2 发布门槛中**；此决定不授予 NAS 生产读写、升级、回滚权限。
- `latest`、`stable` 属于可变发布发现渠道，不可代替不可变 digest；只有用户正式发布授权后才推进。完整来源： [收官 Issue #5](https://github.com/YYxiaoma/PackBreaker/issues/5)、[v1.1.2 研发记录](./v1.1.2-development.md)。
