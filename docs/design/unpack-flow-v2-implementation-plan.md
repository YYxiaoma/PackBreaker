# 数据拆包 v2 实施计划

## 1. 工作包拆分

### WP1：旧拆包模型退场

目标：

- 列出旧 TaskDefinition / TaskExecution / UnpackTask / review 兼容路径；
- 区分拆包领域数据与通用 operation journal；
- 删除不再使用的 API、前端状态和兼容映射；
- 设计破坏性 Alembic migration。

退出条件：

- 仓库不存在“新旧两套拆包定义同时可写”。

### WP2：新 schema 与领域状态机

实现：

- unpack_definitions；
- unpack_executions；
- unpack_execution_items；
- unpack_match_candidates；
- unpack_review_decisions；
- unpack_definition_selected_sources；
- 枚举与 transition guard；
- repository。

退出条件：

- fresh DB；
- v1.0.14 测试 DB destructive migrate；
- 状态机 unit tests。

### WP3：目录树与统一文件过滤

实现：

- AuthorizedPathScope tree service；
- roots / children API；
- opaque node token；
- suffix normalization；
- 目录 / downloader 统一 file filter；
- 手动目录保存前 source scan；
- source scan 分页 / 过滤 / 选择集合；
- ALL_MATCHING_MEDIA / SELECTED_MEDIA 执行范围持久化。

退出条件：

- path escape / symlink / unauthorized root 全部 fail-closed；
- 原型目录树可接真实 API；
- 选片过滤不会丢失跨页 / 跨过滤条件选择。

### WP4：分页媒体发现

实现：

- directory cursor；
- downloader cursor；
- stable source_object_key；
- source snapshot；
- discovery watermark；
- execution counters。

退出条件：

- 10k 合成目录无重复、无遗漏；
- 中断可恢复。

### WP5：批量匹配与候选代次

实现：

- MatchCoordinator；
- query strategy；
- per-site concurrency；
- candidate generation；
- CandidateScorer exact rule；
- 自动匹配阈值；
- candidate evidence；
- timeout/error classification。

退出条件：

- 自动匹配 / review / timeout / error 四类状态可稳定产生；
- 阈值边界和 hard conflict 有单元测试。

### WP5.5：torrent 内容验证

实现：

- 站点 fetch_torrent 接入；
- v1 piece SHA-1；
- v2 Merkle SHA-256；
- hybrid 双验证；
- FULL_VERIFIED / CLIENT_CHECK_REQUIRED / BLOCKED；
- 内容不一致回退审核；
- 主媒体 / 可补齐辅助文件分类；
- qB / Transmission selective-files capability；
- 隔离 staging 补齐 `.nfo` / 海报 / 字幕等辅助文件；
- 站点 verification candidate budget。

退出条件：

- 只有 FULL_VERIFIED 才进入自动辅种；
- info hash 不被误当作影片内容 hash；
- 缺少可补齐辅助文件时不会直接判定主影片不匹配；
- v1 跨文件边界 piece 的补齐写入不会触碰源影片 inode；
- 内容不一致不产生任何外部副作用。

### WP6：人工审核

实现：

- candidates GET；
- default_candidate_id；
- review PUT；
- If-Match；
- Idempotency-Key；
- NO_MATCH；
- stale generation。
- 自动匹配成功项允许人工复核；
- 已进入外部副作用后禁止直接改候选。

退出条件：

- 最高分默认选中但 GET 零写入；
- 并发审核不会覆盖。

### WP7：item 级重试

实现：

- 自动 retry；
- 手动 retry；
- generation + 1；
- retry_count；
- 达上限停止。

退出条件：

- 单 item 重试不会重扫全部来源或重复其他 item。

### WP8：安全执行器接入

实现：

- 从 approved candidate 生成现有安全 Execution Plan；
- operation journal；
- qB / Transmission；
- selective wanted / unwanted file 操作；
- verify / reconcile / rollback；
- execution 聚合。

退出条件：

- 匹配层重构不降低现有文件 / 下载器安全门。

### WP9：任务中心前端重构

实现：

- 两张一级卡；
- 数据拆包列表；
- 新建五区块表单；
- “保存”替代“保存并执行”；
- 新建任务默认“待执行”；
- 任务列表独立“执行”按钮；
- 手动目录任务保存时“全部影视文件 / 选择影片”确认；
- 影视文件选择弹窗与顶部过滤；
- 自动匹配阈值；
- 监控下载器来源及名称 / 分类 / 标签过滤；
- 监控 Cron 辅助预设与五段选择器；
- 目录树 dialog；
- execution 六阶段视图；
- metrics；
- review dialog；
- 自动匹配成功项“审核”按钮；
- 异常 retry；
- 全部可见状态 / 阶段 / 错误中文化。

退出条件：

- 与 docs/prototypes/unpack-flow-v2.html 交互一致；
- desktop / mobile / dark mode E2E；
- 保存动作不会发起 execution，执行按钮才进入运行链。

### WP10：在线升级诊断修复

实现：

- 前端 unknown result 分类 helper；
- structured 5xx 直接展示；
- transient updater 保留 DockerUpdaterError code；
- 回归测试。

退出条件：

- 结构化 503 不再显示“结果暂时未知”。

### WP11：清理与文档同步

删除：

- 旧 API；
- 旧 DTO；
- 旧前端组件分支；
- 旧状态映射；
- 已失效测试。

同步：

- OpenAPI；
- docs/README；
- architecture / domain-model / api / testing 中与旧拆包模型冲突的章节；
- README 功能说明。

## 2. 推荐提交序列

建议按主题拆分：

1. docs: define v1.0.15 unpack v2 architecture
2. refactor: replace legacy unpack persistence model
3. feat: add authorized file tree and extension filters
4. feat: add paged unpack discovery
5. feat: add batched candidate matching
6. feat: add unpack candidate review
7. feat: add item-level match retry
8. refactor: connect unpack v2 to safe execution journal
9. feat: rebuild task center for data unpack
10. fix: surface deterministic online upgrade failures
11. test: complete unpack v2 browser and container gates
12. chore: remove legacy unpack compatibility code

不要把 schema、执行副作用和整套前端塞进一个不可审查的大提交。

## 3. 开发阶段测试节奏

每个 WP 至少运行与改动直接相关的测试；WP8 起持续运行完整后端安全回归。

WP9 起每次交互修改至少运行：

- Vitest；
- vue-tsc --noEmit；
- check-prototype / 专用 unpack-v2 E2E。

进入候选阶段前运行：

- scripts/check.py；
- scripts/test.py。

## 4. 决策冻结点

以下产品规则视为已冻结，除非用户重新确认：

- 顶部只有“数据拆包 / 数据去重”；
- 手动 / 监控共用匹配执行链；
- 保存与执行严格分离，新建任务状态为“待执行”；
- 列表提供独立“执行”按钮；
- 手动 + 目录保存时必须确认“全部影视文件 / 选择影片”；
- 选择影片时支持顶部过滤，且只有最终勾选项进入 SELECTED_MEDIA；
- 后缀过滤存在；
- 高级规则包含自动重试、次数和自动匹配阈值；
- 目录选择使用树形弹窗；
- 达到任务配置阈值即可自动选择候选；
- 低于阈值进入人工审核；
- 自动匹配只决定候选选择，真正辅种必须 FULL_VERIFIED；
- 自动匹配成功项仍允许进入人工审核；
- 审核默认选择最高分但不自动提交；
- 匹配超时 / 匹配错误提供 item 级重试；
- 监控拆包支持下载器来源；
- 监控 Cron 支持可点击辅助输入，同时保留直接编辑；
- 主影片一致但缺少 `.nfo` / 海报等白名单辅助文件时允许安全补齐后继续辅种；
- 全部前端业务状态使用中文；
- 数据去重本轮不重构。

## 5. 可在实现阶段校准的参数

以下不是产品冻结值：

- discovery page size；
- match batch size；
- 单站点并发数；
- 站点搜索 timeout；
- size exact-match 容差；
- candidate retention；
- execution 完成时 NO_MATCH / ERROR 策略。

这些参数必须有默认值、上限和测试，不能散落为 magic number。

## 6. 完成后的删除清单

正式切换新模型后检查并删除：

- 旧手动 / 监控顶层卡片；
- 旧 create type 三卡逻辑；
- 旧 directory drawer；
- 旧高级规则 UI；
- 旧 candidate review 兼容；
- 旧 task definition source_config 不再使用字段；
- 旧状态翻译映射；
- 旧拆包 API router；
- 旧 ORM model / repository；
- 仅为旧 schema 存在的 runtime compatibility 分支。

Alembic 历史 migration 是否物理重建由本版本迁移实施时一次性决定；不能留下多个互相矛盾的 head。
