# M2 退出清单

本清单用于区分 M2「代码能力已经实现」与「仍依赖真实环境/真实语料才能完成的正式验收」。需求真源仍为 `自动拆包辅种系统-需求基线-v0.3.html`，本文件不降低其中任何安全门。

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

全量 Python pytest、前端 Vitest 与前端 production build 均作为 M2 代码收口门禁执行。CI 仍应作为合并后的最终独立证据。

## 3. 尚未满足的正式 M2 退出条件

以下项目不能用合成代码测试替代，需要用户提供真实但可脱敏的环境信息或验收语料：

1. **真实大包语料**：已收到一份代表性真实大包原始 torrent，原始文件不入库；脱敏元数据检查确认其为 v1 private torrent，约 3.33 TiB、1400 个文件、16 MiB piece、218111 个 piece，路径未发现空段、`.`/`..`、NUL 或重复项。真实源目录待后续绑定到项目容器后只读扫描，再完成目录映射、piece 验证、preflight 与 execution plan 的端到端验收；必要日志按实际问题再补充。
2. **7 个失败样例**：当前暂缓预置；后续真实测试遇到失败场景时，按触发现象、必要日志/目录/torrent 摘要和已知归因逐例归档，用于形成编号、稳定错误码、处理策略和回归测试。
3. **HHClub 信息**：确认实际站点引擎、搜索入口、鉴权方式、取种方式和自动化限制。HHClub 正式适配属于 M4，但该信息也是 M0 基线仍未关闭的输入。
4. **真实下载器/NAS 矩阵**：首批目标版本已确认为 qBittorrent 5.2.3、Transmission 4.1.3；NAS 文件系统类型、容器内外路径映射和目标目录布局待真实环境挂载/联调时补齐。M2 只用于环境基线确认，不会因此开启写操作。
5. **候选阈值标定数据**：当前延后到真实候选测试阶段收集正确/错误标签和评分证据，形成误报/召回报告；届时应优先导出脱敏候选 ID、评分组成、硬冲突原因与人工正确/错误标签，不需要提供站点凭证明文。在取得“自动误辅种为 0”的充分证据前，系统继续保持人工确认，不启用基于分数的自动批准。

## 4. M2 与 M3 的边界

M2 到此只允许生成证据和无副作用计划。以下能力明确属于 M3，不能为了“完成 M2”提前接入：

- 创建生产目标目录或硬链接。
- operation journal 驱动的真实文件系统副作用。
- qBittorrent 添加/暂停/恢复/校验/跳过校验等任务写接口。
- 进入 LINKING、ADDING、CLIENT_VERIFYING、SEEDING 状态。
- 取消、回滚、启动对账和下载器副作用幂等恢复。

只有 M2 真实语料与阈值验收完成后，才可正式将里程碑标记为完成；在此之前可继续进行 M3 中不依赖真实环境的领域设计，但不得越过下载器写安全门。
