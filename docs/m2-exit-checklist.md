# M2 退出清单

本清单用于区分 M2「代码能力已经实现」与「仍依赖真实环境/真实语料才能完成的正式验收」。现行需求与安全约束以 [研发文档索引](./README.md) 中列出的版本设计、测试、支持矩阵与验收文档为准；本文件不降低其中任何安全门。

## 1. 已完成的代码能力

- 安全 bencode 解码、原始 `info` 字节保留，以及 BitTorrent v1/v2/hybrid 统一元数据模型。
- v1 跨文件逻辑流 piece 校验、v2 BEP 52 Merkle/piece-layer 校验、hybrid 双协议一致性校验。
- padding、零长度、多文件、Unicode 路径与恶意路径输入的失败关闭处理。
- 源 inventory 只读扫描、唯一文件映射、AMBIGUOUS/MISSING 状态、源文件快照与验证缓存失效。
- 媒体 token 规范化、逐步放宽搜索查询、候选评分与硬冲突证据；评分仅排序，不授予执行权限。
- M-Team API Key 与 HDTime/NexusPHP Cookie 只读适配器、凭证同源保护、资源大小上限和共享契约测试。
- Analyze 编排与 `ANALYZING → SEARCHING → MATCHING → VERIFYING → PREFLIGHT` 状态推进，外部 I/O 不占用长数据库事务。
- TaskUnit、Candidate、不可变 preflight snapshot、current/stale 判断，以及真实任务/预演前端聚合。
- 版本化人工审核 revision、人工 AMBIGUOUS 映射、重验证不可变证据和并发变化阻断。
- pre-execution gate：绑定 task/preflight/review/candidate/重验证证据，区分 FULL_VERIFIED 与 CLIENT_CHECK_REQUIRED。
- 无副作用 execution plan：绑定 current+eligible gate 与相同 metainfo digest，只读检查目标树并生成 HARDLINK/CLIENT_FETCH/PADDING/ZERO_LENGTH 动作；目标冲突、父目录异常、符号链接风险和跨设备均失败关闭。
- M2 所有审核、gate 和 execution plan API 均不创建目录、不创建硬链接、不调用下载器写接口，响应固定 `execution_allowed=false`、`side_effects_started=false`。

## 2. 当前自动化验证证据

本地统一静态检查覆盖：仓库安全扫描、Ruff format/check、mypy strict、前端 Prettier、Vue TypeScript typecheck、OpenAPI snapshot 与生成 TypeScript 类型漂移检查。

M2 execution plan 定向测试覆盖：领域 digest/路径/验证等级不变量、API/CSRF、幂等 plan digest、目标树 currentity、Alembic 迁移与 Runtime migration revision。

真实语料验收入口 `scripts/m2_corpus_acceptance.py` 已加入：默认对同一 torrent/source root 做至少 3 轮只读 inventory + mapping，比较稳定 digest；只有显式 `--verify-content` 才运行完整内容验证，并在验证后再次扫描源快照。报告不回显 tracker、source、Info-hash，且固定 `execution_allowed=false`、`side_effects_started=false`。

全量 Python pytest、前端 Vitest 与前端 production build 均作为 M2 代码收口门禁执行。CI 仍应作为合并后的最终独立证据。

## 3. 正式 M2 退出条件与真实验收结论

以下项目不能只用合成代码测试替代；截至 2026-09-14，所需真实但可脱敏的环境/语料证据已经形成。候选分数仍不授予自动执行权限，负样本不足只意味着自动阈值继续关闭，不再把“必须算出一个自动阈值”误作 M2 退出条件。

1. **真实大包语料（关键链路已验收）**：已收到一份代表性真实大包原始 torrent，原始文件不入库；脱敏元数据检查确认其为 v1 private torrent，约 3.33 TiB、1400 个文件、16 MiB piece、218111 个 piece，路径未发现空段、`.`/`..`、NUL 或重复项。真实源目录已绑定到项目容器并通过 `scan_source_inventory` 完成只读扫描；2026-09-13 已用真实目录完成稳定 TaskUnit 识别、站点候选、精确文件映射、完整 v1 piece 验证、current preflight、review、execution gate 与无副作用 execution plan。后续 M3/M4 还基于同一语料完成了单文件和三文件两条真实 Transmission 写链验收，源内容保持不变，仅预期 hardlink link count 变化。
2. **7 个失败样例（已完成）**：当前完成 7 / 7，见 `docs/real-failure-samples.md`。除 qBittorrent 5.2.3 登录响应兼容、ADD/remove stop 异步收敛、M-Team 站点/API origin 混用、M-Team torrent CDN 二跳和同状态 execute 审计事件误判 review bridge 外，2026-09-14 的 repair 样本筛选还真实触发了 HHClub torrent 获取 `ReadTimeout`；该故障稳定归类为 `SITE_UNAVAILABLE`，torrent payload 流程不自动重试，冷却后一次新的显式获取成功。7 例均已有稳定错误码或审计证据、明确归因、处理策略和回归测试。
3. **真实下载器/NAS 矩阵**：首批目标版本已确认为 qBittorrent 5.2.3 / WebAPI 2.15.1、Transmission 4.1.3；真实 `/downloads`、`/downloads2` 映射已进入项目数据根并完成同设备 hardlink 诊断。2026-09-13 qB 登录兼容问题修复后连接与路径映射均为 `OK`，并已完成真实 `FULL_VERIFIED → hardlink → qB ADD(skip-check) → START → DONE`，以及保持 DONE 终态的 `release → qB keep-files remove → journal-owned hardlink/directory rollback` 闭环；最终源文件 device/inode/size/mtime/nlink 恢复到执行前冻结基线。Transmission 也已完成两条真实写链。M2 本身仍只把这些结果作为环境基线，不以此放宽执行安全门。
4. **候选阈值标定数据（M2 标定条件已完成；自动阈值仍禁用）**：2026-09-14 使用 `scripts/export_candidate_calibration.py` 对真实 SQLite 以只读模式导出脱敏证据。16 条 candidate 记录按 unit/site/torrent 去重后为 14 个候选，其中 12 个 `FULL_VERIFIED` 高置信正确样本、0 个内容错误负样本、0 个 `CLIENT_CHECK_REQUIRED`、2 个未知；正确样本评分范围 53.1818–65.0。现有 review `REJECTED` 不视为内容错误标签，因为早期 2001/M-Team 候选虽因 CDN 获取失败被拒绝，后续已证明与 HHClub torrent 的 746 个 piece hash 全部一致。由于无法估计误报率，`recommended_threshold` 继续为 `null`，系统保持人工确认，不启用基于分数的自动批准。当前报告已经满足“收集真实评分数据并形成标定结论”的 M2 条件；没有高置信负样本意味着不能启用自动批准，而不是要求人为制造负样本。详见 `docs/candidate-threshold-calibration.md`。

HHClub 信息项已于真实环境联调中关闭：确认 NexusPHP + Cookie、当前主站 `https://hhanclub.net`、`torrents.php` 搜索、新版 div 卡片结果布局以及 `details.php` / `download.php` 链路；真实 Cookie 的登录、搜索、详情和真实 `.torrent` 获取均已通过。2026-09-13 还使用 HHClub 同版三文件候选完成 7321 个 v1 piece 的 `FULL_VERIFIED`，随后由后续 M4 写链完成目录创建、3 个 hardlink、Transmission add/verify/start 并收敛到 `DONE`。

## 4. M2 与 M3 的边界

M2 到此只允许生成证据和无副作用计划。以下能力明确属于 M3，不能为了“完成 M2”提前接入：

- 创建生产目标目录或硬链接。
- operation journal 驱动的真实文件系统副作用。
- qBittorrent 添加/暂停/恢复/校验/跳过校验等任务写接口。
- 进入 LINKING、ADDING、CLIENT_VERIFYING、SEEDING 状态。
- 取消、回滚、启动对账和下载器副作用幂等恢复。

**M2 关闭判定：已满足。** 真实大包、失败样例、真实下载器/NAS 基线和候选评分标定报告均已有证据。这里的“阈值验收完成”指形成可审查的真实标定结论，并在零误报证据不足时继续关闭自动批准；不要求为了关闭 M2 人为收集错误候选或强行产生数值阈值。M3/M4 后续真实写链已经继续遵守 FULL_VERIFIED、客户端校验、人工确认和源数据保护安全门。
