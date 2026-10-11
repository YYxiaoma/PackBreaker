# PackBreaker 已知限制（更新于 2026-10-11）


### 2026-10-11 最新对账（优先于下文保留的历史阶段快照）

最新正式版仍为 v1.1.1；当前 main=6aa8ec6、已交付候选=3bb0d849。三站真实源文件 Piece 校验已通过 CarPT 1831/1831、HDFans/PTTime 各 7321/7321，合计 16473/16473；与正式辅种 E2E 不等价。root 审计可读，但 1026:100 降权探针因权限被拒；3 个媒体文件各有 3 个硬链接，严禁擅自递归 chmod/chown。下载器路径映射 UNTESTED、目标与 ownership 未验，七新增站仍 PENDING_REAL_VALIDATION。

完整证据与 SHA/digest 请参阅 [当前收官门禁](./v1.1.2-gate-snapshot-20261011.md) 和 [Issue #5](https://github.com/YYxiaoma/PackBreaker/issues/5)。下文的旧 SHA 记录属于历史阶段快照，不得作为当前发行身份。



**最新正式版本为 v1.1.1；v1.1.2 精确源码 `5364c8de` 的同摘要双架构候选已通过 CI、Docker E2E、原生 ARM64 与双注册表交付，但不是正式 Release。** 七新增站、六旧站真实任务、Tracker 根因及恢复仍待验收；详见 [当前收官门禁](./v1.1.2-gate-snapshot-20261011.md) 和 [Issue #5](https://github.com/YYxiaoma/PackBreaker/issues/5)。Synology NAS 现场验收不属本版门槛，不得擅自操作生产环境。

## 1. 尚未完成的站点与内容校验

- PTTime：搜索和详情身份已有只读证据；用户提供的新候选在隔离审计中完成 **7,321/7,321** Piece 校验，但原始私种未进入 WebCodex Runner，尚未通过 PackBreaker 正式任务计划、ownership journal 与下载器 verify 全链路。
- HDFans：用户提供的新候选在隔离审计中完成 **7,321/7,321** Piece 校验；Transmission 中已存在同 InfoHash 做种任务，但没有 PackBreaker ownership journal。禁止认领、重复添加、重新校验或重启该用户任务。独立 Piece 证据不代表 PackBreaker 任务 E2E。
- HDHome、UBits、KeepFrds、BTSCHOOL、PTTime、聆音Club 还需逐站完成正式 PackBreaker 任务全链路验收。Tracker 现有种子在做种不代表新任务的操作流水或受控恢复已经通过。
- 七新增站点 PTerClub、Audiences、SpringSunday、HDDolby、U2、不可躺、CarPT 全部保持 `PENDING_REAL_VALIDATION`，不能打开正式辅种。CarPT 新候选的隔离全 Piece 校验已通过 1,831/1,831，但仍缺正式支持审批、执行计划、目标/ownership 审核及 PackBreaker 客户端校验；详见 [支持矩阵](./support-matrix.md)。
- 如果真实站点内容获取被执行环境拦截，不能换工具、改变请求形态或通过浏览器绕过。需要保留阻断并用符合站点权限的合法验证路径继续。

- HDDolby 新增站点/临时 Probe API 省略超时时采用 Profile 45 秒默认值的修复已随 PR #26 进入最新候选镜像，**尚未正式发布**。历史已保存的 HDDolby 15 秒配置不自动升级覆盖，必须由用户受控修改。

## 2. 站点网络、Tracker 与错误处理

- PTerClub、SpringSunday、U2、HDFans 存在间歇性连接、搜索失败或 `SITE_UNAVAILABLE` 记录；域名可以解析不证明站点稳定，不能将超时误判为 Cookie 永久失效。
- 2026-10-09 单任务只读记录与 2026-10-10 更大范围 Transmission 只读快照统计口径不同，不可相加。2026-10-10 历史快照中的 Tracker **状态条目**：BTSCHOOL 708 条中 705 成功、3 失败（2 超时）；Rousi Pro 3 条全部失败；UBits 460 条中 379 成功、81 失败（64 超时）。这些不是独立种子数或故障根因；不能泄露 passkey、Tracker URL 或凭证。
- RC1 已整合匹配失败/超时重试分类、限流 `Retry-After` 安全等待、Rousi/NexusPHP 临时只读故障恢复和辅助下载器未知写操作对账保护；自动化测试不替代网络维护、下载器掉线、辅助文件已补齐等真实现场的安全恢复验收。
- NexusPHP 搜索超时与网络连接故障原统一归为 `SITE_UNAVAILABLE`；独立本地候选已分离为 `SITE_TIMEOUT` 和 `SITE_UNAVAILABLE` 、修复只读诊断分类并补充辅助文件已有流水恢复回归，隔离全量测试通过，**尚未合入最新候选镜像，也不是 SpringSunday 真实慢响应问题已解决的证据**。
- 站点限流、身份验证、JS Challenge 和验证码不能靠自动浏览器或伪造来源跳过。具有一次性语义的 torrent 下载 token 不允许盲目重试。

## 3. 媒体、下载器和恢复安全

- 源影片默认只读，不截断、不覆盖、不删除。仅在经过明确授权的挂载根内可操作目标文件；跨设备硬链接、符号链接越界、路径穿越一律失败关闭。
- qBittorrent / Transmission 的 ADD/VERIFY/START/REMOVE 必须有 journal 及操作前后身份、路径、哈希与 ownership 证据。已存在外部同 hash 种子不能推定归属；丢失响应或凭证不能靠重发写操作“碰运气”。
- `FULL_VERIFIED` 和对应的 qBittorrent API capability 才允许相应 skip-checking 路径；Transmission 不因独立 Piece 校验而跳过客户端下载器 verify。未来 WebAPI 版本行为变化须重新验收。
- 自动修复隔离和受控清理要求受支持的文件系统 xattr；不支持时转为人工对账或安全阻断，不允许删除身份不明的文件。
- 单容器 Docker Web 在线升级需要具有足够权限的 `docker.sock`，并仅支持可安全重建的容器拓扑。备份与旧镜像可用性不满足时必须进入 `manual_recovery_required`，不能保证任何 NAS 环境都能自动回滚。

## 4. 发布与验收的界线

- v1.1.1 正式双架构身份已验证；v1.1.2 最新候选 `5364c8de` 的双仓库相同不可变索引 `sha256:ed33c29562bced4f6494aeedfd93026e4213858bd145bebed464ce6a2ead820e` 已通过 [Actions #38123159116](https://github.com/YYxiaoma/PackBreaker/actions/runs/38123159116)。它包含 PR #22–#26 的安全和站点修复，PR #21 由等价修复覆盖关闭；候选不能冒充正式 Release。
- 正式 v1.1.2 还需站点 / 内容 / 下载器受控业务验收，完成当前文档与现场证据对账，并以**最终精确源码 SHA**重新执行 CI、原生 ARM64、浏览器、真实 Docker 升级/回滚和不可变双架构镜像核对。外部阻断必须记录并取得明确的范围决定，不能以早期候选冒充最终发布。
- **Synology NAS 现场验收不在 v1.1.2 发布门槛中**；此决定不授予 NAS 生产读写、升级、回滚权限。
- `latest`、`stable` 属于可变发布发现渠道，不可代替不可变 digest；只有用户正式发布授权后才推进。完整来源： [收官 Issue #5](https://github.com/YYxiaoma/PackBreaker/issues/5)、[v1.1.2 研发记录](./v1.1.2-development.md)。

## 5. 持续保留的产品与运维限制

- **自动批准阈值**：已有高置信正确样本不足以估计误报率；在取得足够的高置信错误负样本并完成标定之前，`recommended_threshold=null`，不得把片名、大小、评分或采样 Hash 当成自动批准的完整内容证据。
- **99% repair 现场样本**：修复、inode 隔离和操作流水代码已有自动化证据，但仍缺自然产生的 `CLIENT_CHECK_REQUIRED/RETRY` 真实现场样本；不得破坏源媒体或制造错误来凑齐验收。
- **备份密钥与恢复入口**：SQLite 数据库一致性备份有意不包含 `secret.key`；脱离对应密钥无法解密已保存凭证。数据库离线恢复须停止活动实例并使用维护 CLI，当前不提供直接替换在线数据库的 Web 按钮。
- **运维 API 范围**：管理员会话是当前 Web 管理 API 的认证边界；通用 Bearer/API Token 自动化访问、下载完成 Webhook 的 HMAC/防重放接口仍未正式开放。operation journal 仅支持满足终态、保留期和引用检查的逐条安全 purge，不支持跳过这些检查的批量清理。
- **日志与诊断**：安全诊断 ZIP 默认不包含运行日志；需要独立使用具有权限和脱敏保护的日志查询/导出入口。遇到无法证明的文件归属、Piece、路径或客户端状态时保持阻断并留下可审计记录。
