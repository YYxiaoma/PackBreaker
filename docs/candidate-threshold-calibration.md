# 候选阈值真实语料标定

## 2026-09-14 基线

本页只记录真实候选的脱敏统计与阈值结论，不保存站点凭证、原始 torrent、tracker、媒体路径或远端 torrent ID。可使用 `scripts/export_candidate_calibration.py <packbreaker.db>` 以 SQLite 只读模式重复生成机器可读证据；导出的 `sample_id` 是由 unit/site/torrent 身份单向摘要得到的短 ID。

当前真实数据库共有 16 条 candidate 记录；按 `normalized unit + site + torrent` 去重后为 14 个候选，其中 12 个已由完整内容验证得到 `FULL_VERIFIED`，0 个 `CLIENT_CHECK_REQUIRED`，0 个 `BLOCKED`，2 个仍缺少内容验证结论。12 个高置信正确候选的评分范围为 **53.1818–65.0**。

当前没有任何可作为“内容错误”的高置信负标签，因此**不能计算可用于自动批准的安全阈值，也不能估计自动批准误报率**。现有评分继续只用于排序；`CandidateScore.automatic_action_allowed` 保持 `false`，所有非同 Info-hash 候选继续经过人工确认和/或完整内容验证。

特别注意，review 的 `REJECTED` 不是内容错误标签。早期 `2001: A Space Odyssey` 的 M-Team 候选因当时的 torrent CDN 获取失败而被人工拒绝，但后续在 CDN 修复后对 M-Team/HHClub 两份真实 torrent 进行只读比较，确认文件布局、4 MiB piece length、746 个 piece hash 全部一致。把这种“操作性拒绝”当成负样本会直接污染阈值标定。

当前数据还能反向约束错误阈值：已验证正确候选的最低分只有 53.1818，因此在没有负样本证据时把 55、60 或更高分数直接当作自动批准门槛，不仅无法证明零误辅种，还会漏掉已经被内容校验证明正确的候选。

## 启用自动阈值前的最低证据要求

- 必须新增**明确的内容正确/内容错误标签**，不能从“审核批准/拒绝”或站点下载失败推断内容真伪。
- 负样本必须覆盖同名异版、年份/集号冲突、大小近似但内容不同、同制作组不同编码/音轨，以及高标题相似度的错误候选。
- 报告必须按评分区间给出正负样本数量、误报/漏报，并单独统计 hard conflict；任何候选仍需保持现有文件映射与 piece/client verify 安全门。
- 在真实语料证明“自动误辅种为 0”且样本量足够之前，`recommended_threshold` 保持 `null`，不得开启基于分数的自动批准。

## Repair 真实 E2E 状态

2026-09-14 已对现有真实数据库和代表性大包进行受控筛选：当前仍为 0 个 `CLIENT_CHECK_REQUIRED` 候选、0 个 `RETRY` task。筛选过程中验证了多个跨站同 release、不同 infohash/不同 piece layout 的候选仍可 `FULL_VERIFIED`，因此不以 torrent 身份差异人为制造 repair。真实 repair E2E 标记为**等待天然 `CLIENT_CHECK_REQUIRED/RETRY` 样本**；一旦真实任务自然进入该状态，再执行 inode isolation、客户端下载补齐和二次校验验收。
