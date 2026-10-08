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

当前实现状态：已完成。

实现：

- unpack_definition；
- unpack_execution；
- unpack_execution_item；
- unpack_match_candidate；
- unpack_review_decision；
- unpack_definition_selected_source；
- 保存前临时表 unpack_source_scan / unpack_source_scan_item；
- 枚举与 transition guard；
- repository。

退出条件：

- fresh DB；
- v1.0.14 测试 DB destructive migrate；
- 状态机 unit tests。

### WP3：目录树与统一文件过滤

当前实现状态：已完成。

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

当前实现状态：已完成。

实现：

- directory cursor；
- downloader cursor；
- stable source_object_key；
- source snapshot；
- discovery watermark；
- execution counters；
- 监控 definition 的 next_run_at / last_triggered_at；
- SCHEDULE execution 后台创建与重叠执行抑制；
- 下载器已完成 torrent 冻结、名称/分类/标签过滤、路径映射错误显式化；
- 后台 UnpackDriver 有界推进，浏览器刷新或关闭不影响 discovery。

退出条件：

- 10k 合成目录无重复、无遗漏；
- 中断可恢复；
- SELECTED_MEDIA 在执行前重新核对 device / inode / size / mtime_ns；
- DOWNLOADER 不把 torrent 汇总大小当成影片大小，而是扫描映射后的真实文件系统对象。

### WP5：批量匹配与候选代次

当前实现状态：已完成。

实现：

- MatchCoordinator；
- query strategy：IMDb → 豆瓣 → 标题+年份/集数 → 标题；
- per-site concurrency；
- candidate generation；
- CandidateScorer exact rule；
- 自动匹配阈值；
- candidate evidence；
- timeout/error classification；
- 后台 UnpackDriver 自动推进 MATCH_PENDING；
- 候选列表 GET 与 default_candidate_id；
- UI 默认候选只用于预选，不写入审核批准；
- 自定义媒体后缀不再被旧 TaskUnit 后缀白名单二次否决；
- Season/Specials 目录上下文可恢复裸集数影片身份。

退出条件：

- 自动匹配 / review / timeout / error 四类状态可稳定产生；
- 阈值边界和 hard conflict 有单元测试；
- 显示分数 100.0 不等于 is_exact_match=true；
- 批量匹配结束后 execution 可正常离开 MATCHING，不会被下一次领取误判为冲突。

### WP5.5：torrent 内容验证

当前实现状态：已完成。

#### WP5.5A：只读内容验证（已完成）

实现：

- 站点 fetch_torrent 接入；
- v1 piece SHA-1；
- v2 Merkle SHA-256；
- hybrid 双验证；
- FULL_VERIFIED / CLIENT_CHECK_REQUIRED / BLOCKED；
- 内容不一致回退审核；
- 主媒体 / 可补齐辅助文件分类；
- source snapshot 与授权路径在取种/校验前重新确认；
- piece mismatch 与“证据不足”分开；
- 后台 UnpackDriver 自动推进只读内容验证；
- item/candidate 持久化 metainfo digest、验证等级与辅助文件状态。

#### WP5.5B：辅助文件补齐（已完成）

实现：

- qB / Transmission selective-files capability；
- 隔离 staging 补齐 `.nfo` / 海报 / 字幕等辅助文件；
- 主影片始终保持在原位置，不复制到 staging；
- v2 external-operation journal：intent → effect → reconcile/result；
- add / file-selection / start / stop / remove-keep-files 全部 journal 化；
- add 响应丢失只允许通过 hash + save path + ownership tag/label 恢复；
- staging 辅助文件就绪后，重新组合“本地主影片 + staging 辅助文件”做完整 v1/v2/hybrid 校验；
- 跨文件边界 piece hash 有合成 torrent 回归；
- 目标下载器在 definition 保存时冻结：目录来源必须显式选择，下载器来源默认继承来源下载器。

退出条件：

- 只有 FULL_VERIFIED 才进入自动辅种；
- info hash 不被误当作影片内容 hash；
- 缺少可补齐辅助文件时不会直接判定主影片不匹配；
- 内容不一致不产生任何外部副作用；
- 5.5B 完成后，v1 跨文件边界 piece 的补齐写入不得触碰源影片 inode。

### WP6：人工审核

当前实现状态：已完成。

实现：

- candidates GET；
- default_candidate_id；
- review PUT；
- If-Match；
- Idempotency-Key；
- NO_MATCH；
- stale generation。
- 自动匹配成功项允许人工复核；
- `review_allowed` 由后端按状态 + external-operation journal 统一计算；
- `MATCHED_AUTO / TORRENT_FETCHING / CONTENT_VERIFYING / CONTENT_VERIFIED / PLAN_PENDING`
  在尚无外部副作用时允许人工复核；
- 已进入辅助文件 staging、文件落位或任何 external-operation journal 后禁止直接改候选；
- 审核与只读内容验证并发时，旧 verification claim 必须 stale 丢弃，不能覆盖新审核决定。

退出条件：

- 最高分默认选中但 GET 零写入；
- 并发审核不会覆盖。

### WP7：item 级重试

当前实现状态：已完成。

实现：

- 自动 retry；
- 手动 retry；
- generation + 1；
- retry_count；
- 达上限停止。

退出条件：

- 单 item 重试不会重扫全部来源或重复其他 item。

### WP8：安全执行器接入

当前实现状态：已完成。

实现：

- 从 FULL_VERIFIED candidate 生成 v2 原生冻结 Execution Plan；
- plan digest / item version / candidate generation / metainfo digest / source snapshot 全部冻结；
- v2 external-operation journal；
- `0039_unpack_external_operation`：v2 external-operation journal；
- `0040_unpack_execution_plan`：冻结 execution plan；
- `0041_unpack_execution_state`：可恢复 execution checkpoint；
- qB / Transmission；
- selective wanted / unwanted file 操作；
- HARDLINK / SYMLINK / COPY 通过 SafeFilesystemGateway 原子落位；
- add paused → ownership proof → client verify/recheck → start seeding；
- qB 仅在 HARDLINK + FULL_VERIFIED + capability 允许时使用 skip-checking；
- Transmission 始终要求显式 verify；
- verify / reconcile；
- execution 聚合；
- 不创建旧 `UnpackTask`，v2 最终执行始终绑定 `unpack_execution_item`。

退出条件：

- 匹配层重构不降低现有文件 / 下载器安全门。

### WP9：任务中心前端重构

当前实现状态：已完成。

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
- 自动匹配成功项“审核”按钮，显示与否直接使用后端 `review_allowed`；
- 异常 retry；
- 全部可见状态 / 阶段 / 错误中文化。

退出条件：

- 与 docs/prototypes/unpack-flow-v2.html 交互一致；
- v1.0.15 专用浏览器烟测覆盖 desktop / 390px，并禁止未 mock API 外泄；
- 保存动作不会发起 execution，执行按钮才进入运行链。

### WP10：在线升级诊断修复

当前实现状态：已完成。

实现：

- 前端 unknown result 分类 helper；
- structured 5xx 直接展示；
- transient updater 保留 DockerUpdaterError code；
- 回归测试。

退出条件：

- 结构化 503 不再显示“结果暂时未知”。

### WP11：清理与文档同步

当前实现状态：已完成本地研发收口。

删除：

- 旧 API；
- 旧 DTO；
- 旧前端组件分支；
- 旧状态映射；
- 已失效测试；
- 依赖旧 Task runtime 的浏览器 approval fixture / E2E / Vite 专用配置。

同步：

- OpenAPI；
- docs/README；
- architecture / domain-model / api / testing 中与旧拆包模型冲突的章节；
- README 功能说明；
- CI browser gate，改为自管理 Vite 生命周期的 v1.0.15 unpack-v2 专用 E2E；
- notification outbox schema，移除旧 task/event 外键字段并保留通用 subject 投影。

本地收口验证：

- `scripts/check.py`：通过；
- `scripts/test.py`：后端 1386 passed / 4 skipped，前端 62 passed，生产构建通过；
- `node scripts/check-unpack-v2-ui.cjs`：通过，并覆盖 390px；
- Alembic fresh DB / 历史升级到 head：通过，autogenerate 无 drift。

说明：Candidate Docker E2E、真实 AMD64 updater 与 native ARM64 属于候选/发布阶段门禁，
本地研发收口不把这些尚未执行的发布门禁描述为已通过。

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
