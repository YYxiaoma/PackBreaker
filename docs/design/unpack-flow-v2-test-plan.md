# 数据拆包 v2 测试与验收计划

## 1. 验收目标

本计划验证四件事：

1. 原型确认的产品交互真实落地。
2. 手动 / 监控任务确实共用同一 execution pipeline。
3. 大目录、失败重试、人工审核和进程恢复在后端可持续运行。
4. 重构没有削弱 PackBreaker 已有源数据、路径、下载器和 operation journal 安全门。

## 2. 单元测试

### 2.1 文件过滤

覆盖：

- .mkv / mkv 规范化；
- 大小写；
- 重复 suffix；
- 空数组；
- 非法路径分隔符和 NUL；
- 文件名多后缀；
- 目录不应用 suffix；
- downloader 映射出的实际文件同样应用过滤。

### 2.2 媒体身份解析

覆盖：

- 标题；
- 年份；
- IMDb；
- 豆瓣 ID；
- 分辨率；
- source medium；
- release group；
- 字段缺失；
- 中英文标题；
- 特殊字符。

### 2.3 CandidateScorer

表驱动覆盖：

- 全字段完全一致 → exact；
- 同 IMDb 但分辨率不同 → 非 exact；
- 同标题但外部 ID 冲突 → hard conflict；
- 标题别名；
- 年份冲突；
- 大小差异；
- 制作组不同；
- 分数接近 100 但 exact=false；
- 搜索噪声被相关性门拒绝。

必须证明“显示 100.0 的浮点值”不能替代 is_exact_match=true。

同时覆盖自动匹配阈值：

- 默认 100.0%；
- 96.8% 在阈值 95.0% 时自动选择；
- 96.8% 在阈值 97.0% 时进入人工审核；
- hard conflict 即使分数达到阈值也不能自动选择；
- Show/Season 01/01.mkv 能从目录上下文恢复标题、年份和季集；
- 用户自定义媒体后缀通过 v2 文件过滤后不会被旧扩展名白名单二次拒绝；
- 匹配终态写入后聚合统计必须在 autoflush=False 下仍读取到最新 item 状态；
- 同一批最后一项使 execution 离开 MATCHING 后，批处理正常结束而不是报状态冲突。

### 2.4 状态机

重点转换：

- MATCH_PENDING → MATCHING；
- MATCHING → MATCHED_AUTO；
- MATCHING → REVIEW_REQUIRED；
- MATCHING → MATCH_TIMEOUT；
- MATCHING → MATCH_ERROR；
- MATCHED_AUTO / MATCHED_MANUAL → TORRENT_FETCHING；
- TORRENT_FETCHING → CONTENT_VERIFYING；
- CONTENT_VERIFYING → CONTENT_VERIFIED；
- CONTENT_VERIFYING → CONTENT_MISMATCH；
- CONTENT_VERIFIED → PLAN_PENDING；
- REVIEW_REQUIRED → MATCHED_MANUAL；
- REVIEW_REQUIRED → NO_MATCH；
- MATCH_ERROR / MATCH_TIMEOUT → MATCH_PENDING；
- 已进入 EXECUTING 后禁止普通 review 覆盖；
- CANCELLED / COMPLETED 为终态。

### 2.5 自动重试

覆盖：

- retry disabled；
- max=0；
- max=N；
- 第 N 次成功；
- 达上限保持异常；
- 重启后 retry_count 不丢；
- 手动 retry 与自动 retry 使用同一逻辑。

## 3. API 测试

### 3.1 Definition

- 创建手动任务后状态为“待执行”，且没有 execution；
- 创建监控任务后状态为“待执行”，且尚未启用 Cron；
- 手动任务点击“执行”后才创建 execution；
- 监控任务点击“执行”后才进入启用状态；
- 启用监控后 next_run_at 被计算，last_triggered_at 仍为空；
- 到点后才创建 SCHEDULE execution，并推进下一次 next_run_at；
- 同一定义存在未结束 execution 时不会创建重叠 execution；
- MANUAL 携带 cron 拒绝；
- MONITOR 缺 cron 拒绝；
- 标准 5 段 Cron 接受；
- 6 段 Cron（含秒）拒绝；
- Cron 辅助预设与分段选择生成的表达式能被后端解析；
- 自动匹配阈值范围与基点换算；
- MONITOR + DOWNLOADER 保存 downloader_id / name_contains / categories / tags；
- 下载器监控只冻结下载完成且命中过滤的 torrent；
- 下载器映射成功后扫描真实文件/目录并应用统一 suffix/大小/名称过滤；
- 下载器路径映射失败形成可见 MATCH_ERROR，而不是静默遗漏；
- MANUAL 使用 DOWNLOADER 来源拒绝；
- extensions 规范化；
- 非法目录 token 拒绝；
- 编辑使用强版本；
- 删除定义不删除已有 execution 审计证据。

### 3.1.1 手动目录执行范围

覆盖：

- 点击“保存”而不是“保存并执行”；
- MANUAL + DIRECTORY 保存时必须先决定全部 / 选择影片；
- 选择“是”保存 ALL_MATCHING_MEDIA，不创建 source scan；
- 选择“否”创建只读 source scan；
- source scan items 支持 cursor / q / extension / resolution / selected；
- 选择集合可跨分页、跨过滤条件保留；
- 未勾选任何影片时禁止保存 SELECTED_MEDIA；
- 保存后把 scan selection 物化到 unpack_definition_selected_source；
- 临时 scan 过期后不能继续创建任务；
- ALL_MATCHING_MEDIA 在点击执行时重新发现当时目录中的媒体；
- SELECTED_MEDIA 执行时只处理保存的 source_object_key；
- 选定文件被删除 / 移动 / 越权时只产生对应中文异常，不扩大到目录其他文件。

### 3.2 Directory Tree

- 只返回授权根；
- 子节点按需加载；
- 不允许 /config；
- 不允许系统路径；
- symlink 逃逸 fail-closed；
- stale token；
- search；
- 文件 / 目录类型正确；
- 超大目录响应分页或限制。

### 3.3 Execution / Item

- run 创建 execution；
- 分页 discovery 生成 item；
- 同 source_object_key 不重复；
- cursor 恢复；
- items 按 status 筛选；
- 聚合计数正确。

### 3.4 Review

- 获取候选默认返回最高分 candidate；
- GET 不产生写入；
- APPROVE 后状态 MATCHED_MANUAL；
- NO_MATCH；
- generation stale；
- candidate 不属于 item；
- If-Match stale；
- Idempotency-Key replay；
- 自动匹配成功项可进入人工审核；
- TORRENT_FETCHING / CONTENT_VERIFYING / CONTENT_VERIFIED / PLAN_PENDING 在无外部副作用时可审核；
- 任意 v2 external-operation journal 存在后审核必须阻断；
- 审核与只读验证并发时，旧 verification claim 不得覆盖审核结果。

### 3.5 Retry

- MATCH_TIMEOUT 可 retry；
- MATCH_ERROR 可 retry；
- COMPLETED 不允许 retry；
- retry generation + 1；
- 旧候选仍可审计；
- 同 key 不重复发起站点请求。

## 4. Application / Integration

### 4.1 大目录分页

合成至少数千媒体节点，证明：

- discovery 多页；
- 内存不持有全部对象 DTO；
- cursor 无重复、无遗漏；
- 中途停止后恢复；
- 插入新文件不会导致旧 item 重复。

### 4.2 批量匹配

验证：

- batch size；
- 同站点并发上限；
- 多站点候选合并；
- IMDb / 豆瓣 / title 搜索顺序；
- 候选去重；
- timeout 只影响单 item；
- 单站点错误不丢失其他站点已成功候选。

### 4.3 Torrent 内容验证

当前 5.5A 已落地并自动化覆盖：

- v1 单文件 FULL_VERIFIED；
- 同长度但 piece 内容不一致 → BLOCKED / CONTENT_MISMATCH；
- source snapshot 改变 → 在 piece 验证前阻断；
- 仅缺 .nfo / 图片等辅助文件 → AUXILIARY_FETCHING，而不是影片不匹配；
- 缺失第二个视频文件 → BLOCKED；
- driver 可在匹配后继续推进只读内容验证。

覆盖真实合成 torrent：

- v1 单文件 piece SHA-1 完全一致；
- v1 多文件 piece 跨文件边界；
- v1 任一 piece 不一致 → CONTENT_MISMATCH；
- v2 pieces root / piece layers 完全一致；
- v2 文件内容不一致；
- hybrid 必须同时通过 v1 与 v2；
- info hash 不被误当作文件内容 hash；
- source snapshot 在验证前后变化时阻断；
- CLIENT_CHECK_REQUIRED 不自动进入辅种执行。

补充辅助文件场景：

- 本地只有主影片，torrent 额外包含 `.nfo` / `.jpg` / `.png` 时不直接判定主影片不匹配；
- 缺失辅助文件进入“辅助文件待补齐”；
- qB / Transmission selective-files capability 可把辅助文件设为 wanted；
- v1 piece 跨主影片与辅助文件边界时，所有补齐写入只发生在 staging；
- 使用本地主影片 + staging 辅助文件后可以重新得到 FULL_VERIFIED；
- 缺失另一个视频文件时不能被辅助文件白名单放行；
- 当前下载器不支持 selective files 时返回稳定阻断状态；
- staging 清理遵循 operation journal；
- add 响应丢失仅通过 hash + save path + ownership tag/label 对账；
- 已确认 APPLIED 的外部状态丢失后进入 RECONCILE_REQUIRED，不盲目重放。

### 4.3.1 最终执行与辅种

- FULL_VERIFIED 才能生成冻结 execution plan；
- plan digest / candidate generation / metainfo digest / source snapshot 被绑定；
- HARDLINK 跨设备阻断，SYMLINK/COPY 使用 SafeFilesystemGateway 原子落位；
- 目标存在时 VERIFY_REUSE_OR_STOP fail-closed；
- 文件落位 journal 在副作用前写 intent；
- qB paused add + ownership proof；只有 HARDLINK + FULL_VERIFIED + capability 才允许 skip-checking；
- Transmission 必须显式 verify；
- 需要 client check 的 qB 必须显式 recheck；
- 已存在同 hash 但无 ownership 的 torrent 不得接管；
- add/verify/start 响应丢失只能从可证明 owned 的真实下载器状态收敛；
- 完整确认后才允许 start seeding 并把 item 标记 COMPLETED。

### 4.4 手动 / 监控同管线

建立 MANUAL 与 MONITOR 两个 definition。

要求 execution 创建后进入相同 application service 和状态转换；测试应避免出现第二套 monitor matcher。

### 4.5 分项继续

一个 execution 中：

- item A exact；
- item B review required；
- item C timeout。

验证 A 可继续安全执行，B 保持审核，C 显示重试；execution 聚合状态和计数正确。

## 5. 安全回归

必须复用并扩展既有测试：

- 路径穿越；
- symlink 逃逸；
- 非授权根；
- source snapshot changed；
- inode changed；
- cross-device hardlink；
- target ownership；
- same size different content；
- downloader add / verify / start；
- qB skip-check capability；
- Transmission verify；
- FULL_VERIFIED 才允许自动辅种；
- CLIENT_CHECK_REQUIRED 不得被当成自动成功；
- CONTENT_MISMATCH 不产生下载器 / 文件副作用；
- operation journal intent-before-effect；
- response lost；
- restart reconcile；
- rollback。

“匹配分数达到自动阈值”不能让这些测试被跳过。

## 6. 前端 Vitest

覆盖：

- 顶部只有两张卡片；
- 数据拆包默认选中；
- 手动 / 监控切换；
- 监控才显示执行时间；
- 新增任务主按钮文案为“保存”；
- 保存成功后任务列表显示“待执行”和“执行”按钮；
- 监控执行时间展示 Cron 辅助组件；
- 点击“每 10 分钟”等预设会回填表达式；
- 修改分钟 / 小时 / 日期 / 月份 / 星期分段会重新生成表达式；
- 监控任务可以切换目录 / 下载器来源；
- 下载器来源显示下载器、任务名称、分类、标签；
- 高级规则包含自动重试、次数和自动匹配阈值；
- suffix 输入规范化；
- review 默认选中最高 candidate；
- 自动匹配成功行也显示“审核”；
- 未点击确认不提交 review；
- 所有可见状态、阶段和错误使用中文；
- timeout / error 显示 retry；
- execution metrics 与后端 counts 对齐；
- empty / loading / error；
- 手动目录任务点击保存后出现“是否处理全部影视文件”确认框；
- 选择“否”后进入影视文件选择弹窗；
- 选片弹窗顶部存在关键字、后缀、分辨率、选择状态过滤；
- 过滤列表不会清除其他已勾选项；
- “勾选当前结果 / 取消当前结果”只作用于当前过滤结果。

## 7. 浏览器 E2E

以已确认原型为 UI 基准：

1. 顶部只有“数据拆包 / 数据去重”。
2. 新增任务五个区块顺序正确。
3. 目录树能展开、选择、搜索。
4. 390、1280、1440、1920 布局无异常。
5. 浅色 / 深色主题。
6. execution 展示六阶段 pipeline，包含“种子内容校验”。
7. 达到任务阈值的 item 展示“自动匹配成功”。
8. 自动匹配成功 item 的操作列存在“审核”按钮。
9. 低于阈值 item 展示“待人工审核”。
10. review 候选按分数排序，最高项默认选中。
11. 默认选中不会自动发 review 写请求。
12. 手工选择其他候选后提交正确 candidate_id。
13. 内容不一致显示中文状态且不进入辅种执行。
14. timeout / error 行显示重试并只重试该 item。
15. 监控拆包切换到下载器来源时显示完整过滤配置。
16. 页面不得直接显示 MATCHED_AUTO / CONTENT_VERIFYING / FULL_VERIFIED 等英文状态枚举。
17. 监控任务 Cron 辅助预设与五段选择器可点击并正确回填输入框。
18. 新建手动目录任务点击“保存”会先进入执行范围确认，不会立即执行。
19. 选择“是，全部处理”后列表新增“待执行”任务并显示独立“执行”按钮。
20. 选择“否，选择影片”后打开影视文件选择弹窗，顶部过滤和勾选逻辑正确。
21. 保存选定影片后列表结果列展示选片数量，点击“执行”后才开始匹配。
22. 数据去重入口仍可进入既有去重面板。

## 8. 迁移测试

虽然不保留旧拆包业务数据，仍验证：

- fresh DB → 新 head；
- v1.0.14 测试 DB → 新 head；
- 旧拆包数据被明确清理；
- 非拆包领域数据保留；
- SecretStore 不受影响；
- 站点 / 下载器配置不受影响；
- operation journal 的保留策略按 migration 设计验证；
- 半迁移状态禁止启动。

## 9. 在线升级 Bug 回归

### 9.1 前端错误分类

覆盖：

- 无 HTTP response → unknown；
- 408 → unknown；
- 504 → unknown；
- 泛化无业务 code 的 503 → unknown；
- UPDATER_BOOTSTRAP_FAILED 503 → definite error；
- UPGRADE_MULTI_NETWORK_UNSUPPORTED 503 → definite error；
- UPGRADE_CONFIG_MOUNT_INVALID 503 → definite error；
- 明确失败后 pending request 清理；
- unknown 时保留同一个 Idempotency-Key。

### 9.2 transient updater

人为让 build_replacement_plan 抛出不同 DockerUpdaterError，断言 API 保留稳定 code，而不是全部变成 UPDATER_BOOTSTRAP_FAILED。

### 9.3 Browser E2E

模拟结构化 503：

- 页面显示具体中文错误；
- 不显示“结果暂时未知”；
- 再次点击可创建新的升级请求。

模拟请求发送后连接断开：

- 显示“结果暂时未知”；
- 重试复用原 Idempotency-Key。

## 10. 性能与容量

最低合成场景：

- 10,000 个目录条目；
- 2,000 个媒体 item；
- 每个 item 20 个候选上限场景。

记录：

- discovery 每页耗时；
- match batch p50 / p95；
- DB 查询数；
- SQLite write duration；
- 峰值内存。

本版本不规定绝对 SLA，但禁止明显 O(N²) 查询和一次加载全部候选 / 文件。

## 11. 发布前门禁

必须通过：

- Ruff format/check；
- mypy；
- pytest 全量；
- Vitest 全量；
- Vue TypeScript；
- OpenAPI drift；
- Vite production build；
- Playwright production preview；
- Candidate Docker E2E；
- AMD64 runtime/updater；
- native ARM64；
- git diff --check。

若真实站点凭证不可用，可用离线 adapter fixtures 验证状态机，但不能把 fixture 描述成真实站点验收。

### 11.1 当前本地研发收口结果

截至 v1.0.15 当前工作树：

- `scripts/check.py`：通过；
- 后端 pytest：1386 passed / 4 skipped；
- 前端 Vitest：62 passed；
- Vue TypeScript：通过；
- OpenAPI drift：通过；
- Vite production build：通过；
- v1.0.15 专用 Playwright UI gate：通过；
- migration fresh/head 与历史升级路径：通过。

Candidate Docker E2E、AMD64 runtime/updater 和 native ARM64 仍属于候选镜像/发布阶段，
在真正执行对应门禁之前保持“未验”，不得由本地 fixture 结果替代。

## 12. 现场验收建议

真正需要真实环境证明的重点：

- Synology 大目录树加载；
- 真实挂载根权限；
- qBittorrent / Transmission 路径映射；
- 若允许真实只读站点搜索，验证 IMDb / 豆瓣 / title 三类 query 的返回结构。

未得到用户明确授权时，不下载 torrent、不修改下载器任务、不写真实媒体。
