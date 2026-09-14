# M5 退出清单

本清单用于把 `docs/development-roadmap.md` 中 M5「历史影片与电视剧」的正式退出条件与可重复证据对应起来。M5 完成不改变既有安全边界：历史扫描仍只读 `/data`，materialize 不自动搜站或写下载器，Analyze 不自动批准候选；只有人工审核后的 current execution gate/plan 才能进入 journal-backed 文件系统与下载器写链。

## 1. 正式退出条件

| 条目 | 状态 | 证据 |
| --- | --- | --- |
| 大目录重复扫描只处理新增或变化内容，不重复创建任务 | ✅ | `tests/application/test_history_scans.py::test_history_scan_is_incremental_and_pause_resume_preserves_cursor`、`test_history_scan_materializes_each_file_snapshot_once_and_changed_snapshot_creates_new_task`、`test_history_scan_materialize_is_idempotent_under_concurrent_replay`；`tests/unit/test_history_scanner.py` 覆盖 cursor-aware 分页、旧 cursor 前子树裁剪和目录 symlink no-follow |
| 影片和剧集各完成扫描到辅种闭环 | ✅ | 2026-09-14 真实影片与真实剧集 E2E 均完成 `HistoryScan → materialize → Analyze → 人工审核 → execution gate/plan → hardlink → qB ADD/START → DONE`，详见下文现场证据 |
| 暂停、重启和取消不丢游标，不影响已完成任务 | ✅ | `tests/application/test_history_scan_driver.py` 覆盖只推进显式启动扫描及 cancel 后退出 driver 队列；`tests/api/test_history_scan_api.py::test_history_scan_cancel_preserves_cursor_and_allows_explicit_new_generation`；扫描状态/cursor/generation 持久化在 SQLite，服务重启后由 HistoryScanDriver 继续处理 `SCANNING` 记录 |

## 2. 真实影片 E2E

样本来自用户指定大包 `DouBan_IMDB.TOP250.Movies.Mixed.Collection.20240501.FRDS` 中《肖申克的救赎》目录。历史任务 `f7df9bc8-2ed7-4d5a-9c60-5505f4b75fd1` 完成真实 Analyze 后，M-Team `210639` 候选为 `FULL_VERIFIED`，5 / 5 文件均为 `EXACT_PATH`，人工审核明确批准该候选并拒绝其余候选。

execution plan 为 5 个 `HARDLINK`、0 个 `CLIENT_FETCH`、预计下载 0 字节，目标 qB 保存路径 `/downloads2`。最终 1 条 `CREATE_DIRECTORY`、5 条 `CREATE_HARDLINK`、1 条 `QBITTORRENT_ADD`、1 条 `QBITTORRENT_START` journal 均为 `APPLIED`；5 / 5 目标文件与源文件 device+inode 一致。qB torrent hash 为 `baea2b9be49c339b833c6b4a5b0d82791acba903`，最终 `progress=1.0`、`stalledUP`、做种确认，任务进入 `DONE`。

## 3. 真实剧集 E2E

稳定样本为 `downloads/憨憨保种/Live.to.100.Secrets.of.the.Blue.Zones.S01.2023.2160p.NF.WEB-DL.DDP5.1.HDR.H.265-HHWEB`。HistoryScan `7c8da60a-6fda-4f1c-9613-4736806f8931` 发现 4 集，首次只 materialize S01E01，生成历史任务 `3d8601d2-8840-4e9a-83aa-c320eba7cd33`；Analyze 在目录上下文中稳定识别 S01E01～E04 四个 episode unit。

HHClub `64042` 候选绑定 S01E01 normalized unit key，验证等级为 `FULL_VERIFIED`，4 / 4 文件均为 `EXACT_PATH`；其余三个 `CLIENT_CHECK_REQUIRED` 候选被人工明确拒绝。execution gate 为 `current=true / eligible=true`，execution plan 为 4 个 `HARDLINK`、0 个 `CLIENT_FETCH`、预计下载 0 字节，目标 qB 保存路径 `/downloads`。

正式 `TaskActionService.execute` receipt `0e1dc251-adf2-482f-8cfc-1810f1f2529f` 为 `SUCCEEDED`。最终 1 条 `CREATE_DIRECTORY`、4 条 `CREATE_HARDLINK`、1 条 `QBITTORRENT_ADD`、1 条 `QBITTORRENT_START` journal 全部 `APPLIED`；4 / 4 目标文件与源文件 device+inode 一致。qB torrent hash 为 `2265beddea56101ea6d2540d3cc23983b7f21cd4`，当前保存路径 `/downloads`、`progress=1.0`、`stalledUP`、`seeding=true`，并带 PackBreaker ownership tag；任务最终为 `DONE`。

## 4. 动态源失败关闭现场证据

首个剧集候选 `Jubilee.S01E01...` 在 Analyze 期间出现 E02～E10 的 `.mkv.!qB` 临时文件，source inventory 从扫描时状态发生真实变化。PackBreaker 正确返回 `ANALYSIS_SOURCE_CHANGED`，任务进入 `RETRY`，且保持 0 preflight、0 operation journal；没有为了完成验收而重试动态源、移动源文件或放宽快照校验。

该现场样本证明历史链路在真实下载目录变化时会失败关闭，而不是仅靠名称、大小或已有候选继续推进。

## 5. M5 关闭判定

**2026-09-14：M5 正式退出条件已满足，可以进入 M6「发布与运维闭环」。**

进入 M6 后继续保留历史扫描的 field acceptance：更大目录长期增量运行、更多 Specials/范围集/多版本真实样本、站点与下载器版本变化，以及动态下载目录中的 source-change 失败关闭。任何持续验收都不得通过降低 FULL_VERIFIED、历史快照、source inventory、人工审核、execution gate、path safety、ownership 或 operation journal 要求来完成。
