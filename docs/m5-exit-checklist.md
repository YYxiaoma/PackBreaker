# M5 退出清单

本清单记录 M5「历史能力与电视剧」的退出证据，以及 v0.1.5 将独立历史辅种模块并入统一任务中心后的现状。能力合并不改变既有安全边界：目录扫描只读源数据，普通任务仍需经过 Analyze、人工审核、current execution gate/plan 后才能进入 journal-backed 文件系统与下载器写链。

## 1. 当前能力归属

| 能力 | 当前实现 | 自动化证据 |
| --- | --- | --- |
| 一次性处理既有媒体 | 手动目录任务：扫描预览、用户选择、保存 `device/inode/size/mtime` 快照，执行时重新校验 | `tests/api/test_task_definitions.py::test_directory_browser_preview_and_manual_snapshot_execution_are_safe` |
| 首次覆盖已有内容并持续增量 | 监控目录任务 `initial_scope=INCLUDE_EXISTING`；完整首次 sweep 后继续按 watermark 发现新增/变化对象 | `tests/api/test_task_definitions.py` 的 monitor watermark / INCLUDE_EXISTING 回归 |
| 大目录有界分批与断点续扫 | 通用 `scan_source_inventory_page` + `TaskSchedule.scan_checkpoint` 持久化 cursor/generation；未完成 sweep 自动 continuation | `tests/api/test_task_definitions.py::test_directory_monitor_resumes_large_scan_from_persisted_cursor` |
| 等待稳定期间不漏页 | stability/debounce 未满足时不推进当前 page cursor，后续检查继续处理同一页 | `tests/api/test_task_definitions.py::test_directory_monitor_does_not_advance_cursor_while_page_waits_for_stability` |
| cursor-aware 子树裁剪 | 稳定全路径字典序分页，跳过 cursor 之前整棵子树且 no-follow | `tests/unit/test_source_inventory.py::test_source_inventory_page_uses_stable_cursor_across_nested_directories`、`test_source_inventory_page_resumes_after_cursor_without_rewalking_prior_subtree` |

独立“历史辅种”页面、`/history-scans` API、`HistoryScanService`、`HistoryScanDriver`、专用扫描器与运行时 ORM 已退出。旧版本 Alembic 创建的 `history_scan`、`history_scan_file`、`history_scan_materialization` 表继续保留在历史迁移链中，以保证旧数据库升级兼容；当前运行时不读取、不写入这些表。

## 2. 历史现场证据

2026-09-14 曾使用独立 HistoryScan 实现完成真实影片与真实剧集两条既有媒体辅种 E2E，均经过 `Analyze → 人工审核 → execution gate/plan → hardlink → qB ADD/START → DONE`，并保留源文件不变、目标 hardlink 与 operation journal 的现场证据。这些结果继续作为安全链处理既有媒体的历史现场证据，但不再代表当前产品入口或运行时架构。

同日动态下载目录还真实触发过 `ANALYSIS_SOURCE_CHANGED` 并保持 0 operation journal，证明源 inventory 在分析期间变化时会在副作用前失败关闭；该安全要求继续由普通任务分析链承担。

## 3. M5 关闭判定

**M5 的业务退出条件保持已满足。** v0.1.5 进一步消除了独立历史辅种子系统，把仍有价值的大目录 cursor 扫描能力收敛进监控拆包，把一次性既有媒体处理收敛进手动拆包。后续验收以任务中心的手动/监控任务为准，不再新增 HistoryScan 专用功能或兼容分支。
