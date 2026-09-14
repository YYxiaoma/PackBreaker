# M4 退出清单

本清单用于把 `docs/development-roadmap.md` 中 M4「Transmission、站点可靠性与修复」的正式退出条件与可重复证据对应起来。M4 完成不表示自动候选批准已开启，也不表示为了验收可以人为损坏真实媒体；所有后续阶段继续沿用 FULL_VERIFIED、客户端校验、人工确认、operation journal 与源数据保护安全门。

## 1. 正式退出条件

| 条目 | 状态 | 证据 |
| --- | --- | --- |
| qB/TR 各完成真实端到端任务 | ✅ | 真实 SQLite 中 Klaus、Hail the Judge 的 `TRANSMISSION_ADD/VERIFY/START` 为 `APPLIED` 且任务 `DONE`；The Reader qB 主链 `DONE`；Hotarubi Run #2 完成 `DONE → release`，`QBITTORRENT_REMOVE=APPLIED`，journal-owned hardlink/目录回滚且任务仍为 `DONE` |
| 7 个失败样例有稳定错误码/处理策略/回归测试，至少三类根因 | ✅ | `docs/real-failure-samples.md` RF-001～RF-007；覆盖下载器协议/异步状态、站点 API/CDN/瞬时故障、任务状态/审计语义 |
| repair 写入前目标与源 inode 隔离；空间不足或无法暂停自动阻断 | ✅ | `tests/unit/test_repair_planning.py::test_auto_piece_plan_requires_paused_downloader_and_space`；`tests/application/test_task_repairs.py::test_task_repair_isolation_hands_off_ownership_and_replans_safely`；`test_task_repair_download_rechecks_and_returns_to_done_without_touching_source` |
| 站点异常不产生请求风暴；恢复后可控 half-open | ✅ | `tests/unit/test_site_reliability.py` 覆盖 single-flight、最小请求间隔、有限重试/总 deadline、熔断、单 half-open probe、取消与恢复；RF-007 提供真实 HHClub timeout 零副作用失败关闭证据 |

## 2. Repair 真实现场验收状态

受控 repair 写链已经由自动化测试覆盖 `RETRY → inode isolation → CLIENT_VERIFYING → repair download → stop → 独立 recheck/verify → SEEDING → DONE`，并验证 source inode/size/mtime/字节保持不变。真实数据库截至 2026-09-14 仍为 0 个 `CLIENT_CHECK_REQUIRED`、0 个 `RETRY`；代表性大包受控筛选没有发现适合且安全的天然样本。

因此真实 repair E2E 作为**持续 field acceptance** 保留：真实任务自然进入 `CLIENT_CHECK_REQUIRED/RETRY` 后再运行现场验收。不得为了让该项变绿而修改真实源数据、伪造 RETRY、把不同压制强行映射为同一候选，或放宽 inode/ownership/空间/暂停证明。

## 3. 候选评分与自动化边界

候选评分真实标定见 `docs/candidate-threshold-calibration.md`。当前真实数据能证明 12 个高置信 `FULL_VERIFIED` 正样本，但没有高置信内容错误负样本；因此 `recommended_threshold=null`，`CandidateScore.automatic_action_allowed=false`，继续人工确认。这是明确的安全结论，不是 M4 未完成项。

## 4. M4 关闭判定

**2026-09-14：M4 正式退出条件已满足，可以进入 M5「历史影片与电视剧」。**

进入 M5 后仍保留以下持续验收：自然 repair 样本、候选正负标签积累、新下载器/站点版本兼容和新增真实故障。任何持续验收都不得通过降低 FULL_VERIFIED、源数据保护、路径安全、ownership 或幂等要求来完成。
