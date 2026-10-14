# 真实失败样例登记

本文件只登记真实环境中已经触发、能够脱敏复现并已经形成明确归因的失败样例。未实际出现的推测场景不计入“7 个失败样例”退出条件。

## 当前进度

- 已正式归档：7 / 7。
- 样例来自 2026-09-13 至 2026-09-14 的真实站点联调、任务执行、候选筛选和 qBittorrent 5.2.3 / WebAPI 2.15.1 端到端验收。
- 凭证、Cookie、tracker、原始 `.torrent` 和媒体内容均不进入本文档或普通 CI。

## RF-001：qBittorrent 5.2.3 成功登录被误判为认证失败

- **触发现象**：PackBreaker 连接测试返回 `DOWNLOADER_AUTH_FAILED`，qB 配置保持 disabled；实际凭证在同一实例可正常使用。
- **真实证据**：使用已保存凭证通过应用内部边界探测时，`POST /api/v2/auth/login` 返回 HTTP 204、空正文并下发 `QBT_SID_*` session cookie；随后读取 qB 版本和 WebAPI 版本均为 HTTP 200，分别为 v5.2.3 / 2.15.1。
- **根因**：适配器仍只接受旧式 `200 + Ok.` 登录成功响应，把 5.2.3 的 `204 + session cookie` 错误归类为认证失败。
- **处理策略**：同时接受旧式 `200 + Ok.` 与 `204 + SID/QBT_SID_* cookie`；不接受没有 session cookie 的任意 204。
- **回归测试**：`test_qbittorrent_523_probe_accepts_204_session_cookie_login`、`test_qbittorrent_probe_rejects_204_without_session_cookie`。
- **结果**：真实连接探测恢复 `OK`，路径映射继续保持 `OK`，qB 可通过正式安全门启用。

## RF-002：qB 暂停添加异步收敛导致 ADD journal 进入对账阻断

- **触发现象**：真实 FULL_VERIFIED 三文件任务已在 qB 创建，hash/save path/ownership tag 正确且最终为 `stoppedUP`、100%；但 PackBreaker 停在 `ADDING`，`QBITTORRENT_ADD` journal 为 `RECONCILE_REQUIRED`。
- **真实证据**：`torrents/add` 返回 200；第一次 `torrents/info` 观察到任务未保持停止，PackBreaker 发出 `stop`；约 118 ms 后单次状态读取仍未观察到停止态，于是按安全门进入 `RECONCILE_REQUIRED`。稍后同一真实任务已稳定为 `stoppedUP`。
- **根因**：qB 对暂停/停止状态采用异步收敛；原实现只在 `stop` 后立即读取一次状态，确认窗口过短。
- **处理策略**：只在 hash + ownership tag + save path 始终精确一致时，对 stop 后状态做最多约 2 秒的有界只读轮询；身份或路径漂移立即失败关闭。对已经进入 `RECONCILE_REQUIRED` 且缺少 after snapshot 的 ADD，仅当原 before snapshot 明确证明 torrent 不存在、intent 只有一个确定 info-hash、下载器版本未变、当前 torrent 的 hash/save path/ownership/stopped 全部一致时，公开 reconcile 才允许只读重建 after snapshot 并 CAS 回 `APPLIED`。RECHECK/START 和多 hash 情况仍不允许缺 after snapshot 自动恢复。
- **回归测试**：`test_active_torrent_allows_bounded_stop_convergence_before_applied`、`test_qb_add_reconcile_can_prove_missing_after_snapshot_from_intent_and_current_state`、`test_qb_add_reconcile_missing_after_snapshot_requires_absence_proof`、`test_task_operation_service_reconciles_provable_qb_add_without_after_snapshot`。
- **结果**：真实 ADD journal 经公开 `TaskOperationService.reconcile` 只读恢复为 `APPLIED`，没有再次 add/stop/start/recheck/remove；随后任务进入 `SEEDING`。在 `QBITTORRENT_START` 已生效但 journal 尚为 `INTENT_RECORDED` 的真实窗口重启 PackBreaker，启动恢复仅凭现有 `stalledUP` + 100% + ownership 证据把同一 START journal 收敛为 `APPLIED` 并最终 `DONE`，未重复 start。

## RF-003：M-Team 站点 origin 被错误当作 API origin，导致有效 API Key 被判无效

- **触发现象**：站点配置使用真实主站 `https://kp.m-team.cc` 后，连接测试提示“M-Team API Key 无效或权限不足”，稳定归类为 `SITE_AUTH_FAILED`；同一 API Key 在其他已工作的客户端中可正常使用。
- **真实证据**：联调确认 M-Team API 请求必须发送到 `https://api.m-team.cc`，同时携带 `Origin: https://kp.m-team.cc`；站点配置中的主站 origin 不能直接作为 API origin。
- **根因**：旧适配逻辑没有明确区分“用户配置的站点 origin”和“固定 API origin”，导致连接测试请求目标错误并被远端拒绝，错误表象被误认为凭证无效。
- **处理策略**：`_normalize_mteam_origins()` 只接受 `https://*.m-team.cc` 的无凭证站点 origin，并固定派生 API origin `https://api.m-team.cc`；API Key 只发给 API origin，`Origin` 头仍使用用户配置的站点 origin。
- **回归测试**：`test_mteam_configured_site_origin_is_not_used_as_api_origin`，并由共享 M-Team adapter contract 继续检查 API Key 不会发送给下载域名。
- **结果**：真实 M-Team 连接测试恢复正常，并继续完成真实搜索、详情和 torrent 获取。

## RF-004：M-Team torrent CDN 二跳未被安全跟随，真实候选下载失败

- **触发现象**：真实候选在搜索/详情阶段正常，但下载 `.torrent` 时失败；数据库中的真实候选保留稳定 `error_code=SITE_TORRENT_FETCH_FAILED`。
- **真实证据**：真实 M-Team 下载令牌返回 `api.m-team.cc` URL，随后 HTTP 302 跳转到 `fr1.halomt.com`；旧下载路径没有按安全白名单处理这一第二跳，因此无法取得 torrent payload。
- **根因**：下载 URL 安全模型只覆盖初始 M-Team URL，没有兼容 M-Team 当前使用的受信 CDN 重定向链。
- **处理策略**：关闭自动重定向，最多手工跟随 3 次；每一跳都要求 HTTPS、无 URL 凭证/fragment、显式端口只能是 443，且 host 必须属于 `m-team.cc`、`halomt.com` 或 `groueta.cc` 的允许后缀。API Key 只用于 token API，绝不转发到下载跳。
- **回归测试**：`test_mteam_follows_allowlisted_download_redirect_without_forwarding_api_key`、`test_mteam_rejects_download_redirect_to_untrusted_host`，以及重定向次数上限测试。
- **结果**：真实 M-Team torrent 可以经 `api.m-team.cc → fr1.halomt.com` 获取，同时站外跳转和凭证转发仍被阻断。

## RF-005：同状态 execute 审计事件误判 review bridge 失效

- **触发现象**：真实 Klaus 任务已经完成 PREFLIGHT、人工 review、eligible gate 和 ready execution plan，但第一次正式 execute 返回 `LINKING_PLAN_NOT_CURRENT`，且没有创建文件系统/下载器副作用。
- **真实证据**：失败 action receipt 持久化 `code=LINKING_PLAN_NOT_CURRENT`。当时生命周期最后一次真实迁移是 `PREFLIGHT → AWAITING_CONFIRMATION / REVIEW_OPENED`，随后 `TaskActionService` 在执行前追加了同状态 `TASK_EXECUTE_REQUESTED` 审计事件；旧 bridge 校验把绝对 latest event 当成 latest transition，因此被自己刚写入的审计事件否定。
- **根因**：生命周期桥校验混用了“最近任意事件”和“最近状态迁移事件”两个概念。
- **处理策略**：增加 `TaskRepository.latest_transition_event()`，只选择 `from_status IS NOT NULL` 且 `from_status != to_status` 的真实生命周期迁移；review/preflight bridge 使用该查询，同状态审计仍保留在完整事件流中。
- **回归测试**：`test_latest_transition_event_ignores_same_state_action_audit`；同时保留 linking 的 stale-plan 失败关闭测试，确保真正的 review/gate/target 漂移仍返回 `LINKING_PLAN_NOT_CURRENT`。
- **结果**：同一真实 Klaus 链重新生成 current 证据后可进入 LINKING 并最终由 Transmission 收敛到 `DONE`；审计事件不再自我使计划 stale。

## RF-006：qB remove 的 stop 异步收敛阻断 DONE 资源释放

- **触发现象**：真实 Hotarubi Run #2 已经 `DONE` 且由 PackBreaker 明确拥有 qB torrent、1 个 hardlink 和 1 个目录；显式 post-DONE `release` 在 remove 前先请求 stop，但第一次立即读取仍观察到 torrent 未停止，动作以 `DOWNLOADER_STOP_NOT_CONFIRMED` 失败关闭。此时 `QBITTORRENT_REMOVE` journal 保持 `INTENT_RECORDED`，qB torrent 随后自行稳定到 `stoppedUP`，文件系统资源完全未回滚。
- **真实证据**：失败后只读检查确认同一 remove journal 没有 after snapshot，qB 中 torrent 仍存在但已经 `stoppedUP`、不再做种，目标 hardlink 仍存在；因此失败点严格位于 remove 之前，没有发生“下载器仍持有路径但文件已被删除”的危险顺序。
- **根因**：qB 5.2.3 的 stop 状态同样采用异步收敛；ADD 侧已经有有界 stop 确认，而 REMOVE 侧仍只在 stop 后立即读取一次状态。
- **处理策略**：REMOVE 复用与 ADD 相同的最多约 2 秒有界只读 stop 收敛窗口；每次读取仍通过既有 hash/save path/ownership 证明，torrent 消失则按响应未知后的安全恢复处理，身份漂移仍失败关闭。超出窗口继续保留 `INTENT_RECORDED`，后续重试先查询真实状态，不盲目重复 remove。
- **回归测试**：`test_remove_waits_for_async_stop_convergence_before_removing`，并继续由 `test_remove_stops_active_owned_torrent_before_removing`、remove 响应丢失和并发幂等测试覆盖原语义。
- **结果**：真实 `INTENT_RECORDED` remove journal 在 qB 已收敛到 `stoppedUP` 后安全恢复并完成 keep-files remove；随后 PackBreaker 只回滚 journal-owned hardlink/目录。最终 qB hash 不存在，目标目录不存在，源文件 device/inode/size/mtime/nlink 与执行前冻结基线逐项一致，task 保持 `DONE`。同一个成功 release Idempotency-Key 重放返回同一 receipt，remove journal 和 `TASK_RESOURCES_RELEASED` 事件都仍只有 1 条。

## RF-007：HHClub torrent 获取瞬时超时，安全失败关闭且不自动重放下载链

- **触发现象**：2026-09-14 在真实 repair 样本筛选中，HHClub 搜索已经返回有效候选，但读取该候选详情/`.torrent` 时底层 HTTP 客户端真实触发 `ReadTimeout`；适配器稳定归类为 `SITE_UNAVAILABLE`，没有生成候选 metainfo、task 写链、hardlink 或下载器副作用。
- **真实证据**：失败栈位于 NexusPHP `details.php` / torrent 获取只读链，底层超时被 `NexusPHPAdapter` 转换成 retryable `SiteAdapterError(code="SITE_UNAVAILABLE")`。没有对同一下载链立即循环重试；经过冷却后只进行一次新的显式获取，同一真实候选成功返回有效 torrent 并完成文件清单解析。
- **根因**：远端站点/链路发生瞬时读取超时，并非凭证失效、torrent 不存在或本地数据问题。该类失败不可通过重复消费当前下载链来证明安全成功。
- **处理策略**：NexusPHP 网络/读取超时统一映射为稳定 `SITE_UNAVAILABLE`；`SiteReliabilityRegistry.fetch_torrent()` 保持单次调用、不自动重试 torrent payload 流程，避免一次性下载参数或远端状态被隐式重复消费。上层可以在新的显式操作/后续任务中重新获取 fresh metainfo；search/details 的普通可靠性策略和 circuit breaker 仍独立工作。
- **回归测试**：`test_hhclub_torrent_timeout_maps_to_retryable_site_unavailable` 精确覆盖 NexusPHP timeout → `SITE_UNAVAILABLE`；`test_site_reliability_does_not_retry_torrent_token_flow` 继续证明 torrent 获取失败时可靠性层不会自动发起第二次下载调用。
- **结果**：真实筛选在 timeout 时零副作用失败关闭，后续单次显式重试成功；没有为了完成样例数量而放宽 retry、安全门或凭证隔离策略。

## 结论

7 个样例已全部来自真实触发，并覆盖下载器协议兼容/异步状态、站点 API/下载链路、外部瞬时故障和任务状态/审计语义等根因。该退出条件完成不代表取消后续真实故障收集；新的真实问题仍按相同格式继续追加，但不再为了凑数量主动制造失败。
