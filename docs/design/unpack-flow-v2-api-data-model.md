# 数据拆包 v2 API 与数据模型设计

## 1. 设计原则

1. API 面向统一“数据拆包”，不再按手动 / 监控复制资源。
2. definition 与 execution 分离。
3. 每个媒体对象是独立 execution item。
4. 候选列表持久化，人工审核不依赖前端缓存。
5. 所有写接口使用 CSRF + Idempotency-Key 或强版本。
6. 旧拆包 API 在前端迁移完成后删除，不保留兼容别名。
7. 当前为测试阶段，允许破坏性重建旧拆包表，不要求历史数据保真。

## 2. 表结构

### 2.1 unpack_definitions

核心字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | UUID/text | 主键 |
| name | text | 任务名称 |
| trigger_kind | enum | MANUAL / MONITOR |
| status | enum | PENDING_EXECUTION / ENABLED / PAUSED / ERROR |
| source_kind | enum | DIRECTORY / DOWNLOADER |
| source_config_json | json/text | 来源配置 |
| execution_scope_kind | enum | ALL_MATCHING_MEDIA / SELECTED_MEDIA |
| file_filter_json | json/text | 文件过滤 |
| site_ids_json | json/text | 扫描站点 |
| output_config_json | json/text | 输出、存放方式、冲突策略 |
| retry_enabled | bool | 自动重试 |
| max_retries | int | 最大自动重试 |
| auto_match_threshold_bps | int | 自动匹配阈值，0..10000，默认 10000 |
| cron_expression | text nullable | 监控任务 |
| timezone | text nullable | 监控任务 |
| version | int | 乐观锁 |
| created_at | UTC | 创建时间 |
| updated_at | UTC | 更新时间 |

约束：

- MANUAL 时 cron 必须为空；
- MONITOR 时 cron / timezone 必须有效；
- 新建任务默认 status = PENDING_EXECUTION；
- MANUAL 当前仅允许 DIRECTORY 来源；MONITOR 允许 DIRECTORY / DOWNLOADER；
- SELECTED_MEDIA 仅允许 MANUAL + DIRECTORY；
- DOWNLOADER 来源必须包含 downloader_id，可选 name_contains / categories / tags；
- max_retries 设上限，例如 10；
- auto_match_threshold_bps 必须位于 0..10000；
- JSON 在 application 层使用 Pydantic 强类型，不允许自由字典直接进入领域层。

### 2.2 unpack_executions

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | UUID/text | 主键 |
| definition_id | FK | 所属定义 |
| trigger_kind | enum | MANUAL / SCHEDULE / MONITOR_EVENT |
| status | enum | 当前聚合状态 |
| config_snapshot_json | json/text | 完整冻结配置 |
| discovery_cursor | text nullable | 后台发现 cursor |
| discovery_complete | bool | 是否完成发现 |
| total_count | int | 已发现总数 |
| matched_auto_count | int | 自动确认 |
| review_count | int | 待人工审核 |
| content_verified_count | int | 内容校验通过 |
| content_mismatch_count | int | 内容不一致 |
| timeout_count | int | 匹配超时 |
| error_count | int | 匹配错误 |
| completed_count | int | 完成对象 |
| started_at | UTC | 开始 |
| finished_at | UTC nullable | 结束 |
| version | int | 乐观锁 |

索引：

- definition_id + started_at；
- status + updated_at。

### 2.3 unpack_execution_items

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | UUID/text | 主键 |
| execution_id | FK | execution |
| source_object_key | text | 源对象稳定身份 |
| source_snapshot_json | json/text | 路径、size、mtime、inode 等 |
| media_identity_json | json/text | title/year/imdb/douban 等 |
| status | enum | item 状态 |
| selected_candidate_id | FK nullable | 已批准候选 |
| candidate_generation | int | 当前候选代次 |
| retry_count | int | 匹配重试次数 |
| content_verification_level | enum nullable | FULL_VERIFIED / CLIENT_CHECK_REQUIRED / BLOCKED |
| torrent_metainfo_digest | text nullable | 已验证候选 torrent 摘要 |
| auxiliary_state_json | json/text nullable | 缺失辅助文件、staging 与补齐状态 |
| last_error_code | text nullable | 稳定错误码 |
| last_error_message | text nullable | 脱敏中文错误 |
| match_started_at | UTC nullable | 匹配开始 |
| match_finished_at | UTC nullable | 匹配结束 |
| version | int | CAS |
| created_at | UTC | 创建 |
| updated_at | UTC | 更新 |

唯一约束：

- execution_id + source_object_key。

### 2.4 unpack_match_candidates

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | UUID/text | 主键 |
| item_id | FK | execution item |
| generation | int | 候选代次 |
| site_id | FK | 来源站点 |
| candidate_key | text | 站点候选稳定键 |
| title | text | 候选标题 |
| size_bytes | int nullable | 大小 |
| imdb_id | text nullable | IMDb |
| douban_id | text nullable | 豆瓣 |
| seeders | int nullable | 做种数 |
| score | decimal/int | 原始精度分数 |
| is_exact_match | bool | 领域规则完全匹配 |
| evidence_json | json/text | 结构化评分证据 |
| verification_status | enum | NOT_CHECKED / VERIFYING / VERIFIED / MISMATCH / UNAVAILABLE |
| verification_level | enum nullable | 内容验证等级 |
| verification_error_code | text nullable | 内容验证稳定错误码 |
| metainfo_digest | text nullable | 候选 torrent metainfo 摘要 |
| raw_ref_json | json/text | 后续取种所需安全引用，不保存秘密 |
| created_at | UTC | 创建 |

唯一约束：

- item_id + generation + site_id + candidate_key。

### 2.5 unpack_review_decisions

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | UUID/text | 主键 |
| item_id | FK | item |
| generation | int | 审核候选代次 |
| decision | enum | APPROVE / NO_MATCH |
| candidate_id | FK nullable | APPROVE 时必填 |
| item_version_before | int | 并发证据 |
| decided_at | UTC | 时间 |

审核历史建议 append-only，不覆盖旧 decision。

### 2.6 unpack_definition_selected_sources

仅用于手动目录任务的 `SELECTED_MEDIA` 执行范围。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | UUID/text | 主键 |
| definition_id | FK | 所属任务定义 |
| source_object_key | text | 保存时选中的稳定媒体身份 |
| canonical_path_hint | text | 仅用于展示 / 重新定位，不作为授权依据 |
| filename | text | 展示名称 |
| size_bytes_at_selection | int nullable | 保存时大小提示 |
| created_at | UTC | 保存时间 |

唯一约束：definition_id + source_object_key。

## 3. API

### 3.1 任务定义

资源：

- GET /api/v1/unpack/definitions
- POST /api/v1/unpack/definitions
- GET /api/v1/unpack/definitions/{definition_id}
- PUT /api/v1/unpack/definitions/{definition_id}
- DELETE /api/v1/unpack/definitions/{definition_id}
- POST /api/v1/unpack/definitions/{definition_id}/actions

actions：

- run；
- pause；
- resume。

新建定义只保存配置并进入 `PENDING_EXECUTION`，不得自动创建 execution。

run 语义：

- MANUAL：从 `PENDING_EXECUTION` 创建新的 execution；
- MONITOR：将定义切换为 `ENABLED`，之后按 Cron / 来源规则创建 execution；
- 同一个手动任务已有非终态 execution 时，重复 run 必须拒绝或按同一个 Idempotency-Key 回放。

### 3.1.1 手动目录影视文件扫描与选择

当用户在“是否处理目录下全部影视文件”选择“否”时使用临时扫描资源：

- POST /api/v1/unpack/source-scans
- GET /api/v1/unpack/source-scans/{scan_id}/items
- PUT /api/v1/unpack/source-scans/{scan_id}/selection
- GET /api/v1/unpack/source-scans/{scan_id}/selection-summary

`POST source-scans` 输入目录 selection token 与当前文件过滤配置，只执行只读扫描，不创建任务、不创建 execution。

items 查询支持：

- cursor；
- limit；
- q；
- extension；
- resolution；
- selected = true / false。

selection 接口按 source_object_key 增删勾选集合。最终创建 definition 时传 `execution_scope_kind=SELECTED_MEDIA` 和 `scan_id`，服务端把最终选择集合物化到 `unpack_definition_selected_sources` 后销毁 / 过期临时 scan。

若用户在确认框选择“是”，则不需要创建 source scan，直接保存 `execution_scope_kind=ALL_MATCHING_MEDIA`。

### 3.2 Execution

资源：

- GET /api/v1/unpack/executions
- GET /api/v1/unpack/executions/{execution_id}
- GET /api/v1/unpack/executions/{execution_id}/items
- POST /api/v1/unpack/executions/{execution_id}/actions

actions：

- pause；
- resume；
- cancel。

items 列表支持：

- cursor；
- limit；
- status；
- q；
- site_id。

### 3.3 候选

GET /api/v1/unpack/items/{item_id}/candidates

响应必须包含：

- item_id；
- generation；
- item_version；
- default_candidate_id；
- candidates。

每个 candidate 至少包含：

- id；
- site_id；
- title；
- score；
- is_exact_match；
- evidence；
- verification_status；
- verification_level。

default_candidate_id 只用于 UI 默认选中，不表示数据库已经批准。

### 3.4 人工审核

PUT /api/v1/unpack/items/{item_id}/review

必须携带：

- If-Match；
- Idempotency-Key。

批准请求字段：

- decision = APPROVE；
- candidate_id；
- generation。

无匹配请求字段：

- decision = NO_MATCH；
- generation。

服务端要求：

- item 当前为 REVIEW_REQUIRED，或处于尚未产生外部副作用的 MATCHED_AUTO；
- generation 等于当前 generation；
- candidate 属于当前 item + generation；
- If-Match 等于 item version；
- 重复 Idempotency-Key 返回相同结果。

审核修改自动匹配结果时，服务端必须使旧的未执行计划和旧内容验证证据失效；若 item 已经进入外部副作用阶段，返回稳定冲突错误，要求先取消并安全回滚。

### 3.5 item 重试

POST /api/v1/unpack/items/{item_id}/actions

action：

- retry_match。

允许状态：

- MATCH_TIMEOUT；
- MATCH_ERROR；
- NO_MATCH（如果最终产品允许重新搜索）。

成功后：

- candidate_generation + 1；
- 状态回到 MATCH_PENDING；
- 保留旧 generation 候选作为审计历史。

### 3.6 文件 / 目录树

资源：

- GET /api/v1/files/tree/roots
- GET /api/v1/files/tree?parent_token=...&query=...

根响应只包含管理员显式授权挂载。

node 字段：

- token；
- name；
- type；
- has_children；
- child_count（可选）；
- estimated_size_bytes（可选）。

任务保存时传 selection token，而不是让前端自由提交任意绝对路径。服务端解析 token 后得到 canonical path 并再次执行 AuthorizedPathScope 校验。

如果保留手工路径输入，也必须走同一授权校验。

## 4. Definition 请求模型

请求逻辑结构：

- name；
- trigger_kind；
- site_ids；
- source；
- execution_scope_kind；
- file_filter；
- output；
- retry；
- matching；
- cron_expression / timezone（仅 MONITOR）。

cron_expression：

- 只接受标准 5 段 Cron：分钟、小时、日期、月份、星期；
- 不接受秒字段；
- 前端 Cron 辅助组件只负责生成表达式，服务端仍必须独立解析和校验；
- timezone 使用任务定义保存的 IANA 时区。

file_filter：

- file_types；
- extensions；
- min_size_bytes；
- max_size_bytes；
- include_name；
- exclude_names；
- ignore_temp_files；
- include_subdirectories。

output：

- selection_token；
- storage_mode；
- conflict_policy。

retry：

- enabled；
- max_attempts。

matching：

- auto_match_threshold_bps，0..10000；
- API 使用整数基点避免浮点边界，例如 96.8% = 9680；
- 前端以百分比展示并使用中文字段名“自动匹配阈值”。

MONITOR + DOWNLOADER 的 source：

- downloader_id；
- name_contains；
- categories；
- tags。

服务端固定只处理下载完成且文件状态稳定的下载器任务，该安全条件不作为用户可关闭选项。

MANUAL + DIRECTORY：

- ALL_MATCHING_MEDIA：保存时不固化目录文件清单，点击执行时按当时目录内容和文件过滤规则重新发现；
- SELECTED_MEDIA：创建定义时必须提供有效 scan_id 且至少选择 1 个媒体；点击执行时只处理已保存 source_object_key，并重新校验路径授权与文件存在性。

## 5. 统一错误码

建议新增稳定业务码：

- UNPACK_DEFINITION_INVALID
- UNPACK_SOURCE_UNAVAILABLE
- UNPACK_DISCOVERY_FAILED
- UNPACK_ITEM_STALE
- UNPACK_MATCH_TIMEOUT
- UNPACK_MATCH_FAILED
- UNPACK_TORRENT_FETCH_FAILED
- UNPACK_CONTENT_MISMATCH
- UNPACK_CONTENT_VERIFICATION_UNAVAILABLE
- UNPACK_AUXILIARY_FETCH_UNSUPPORTED
- UNPACK_AUXILIARY_FETCH_FAILED
- UNPACK_REVIEW_REQUIRED
- UNPACK_REVIEW_STALE
- UNPACK_CANDIDATE_STALE
- UNPACK_RETRY_NOT_ALLOWED
- UNPACK_EXECUTION_PAUSED
- UNPACK_EXECUTION_CANCELLED
- FILE_TREE_ROOT_FORBIDDEN
- FILE_TREE_NODE_STALE
- FILE_EXTENSION_INVALID

第三方站点错误必须映射为 PackBreaker 稳定码，不把第三方正文直接透传给 Web。

## 6. OpenAPI 与前端类型

API 落地后同一提交更新：

- backend Pydantic schemas；
- frontend/openapi.json；
- frontend/src/api/generated/schema.ts；
- 前端 API wrappers；
- contract tests。

前端不得手写与 OpenAPI 重复的 DTO。

## 7. 破坏性迁移方案

建议 migration：

1. 删除旧 task definition / execution / candidate review 的拆包专属数据；
2. 建立新六表；
3. 保留 operation journal、下载器、站点、通知等独立领域表；
4. 不迁移旧 task records；
5. fresh DB 和 v1.0.14 测试 DB 都能到新 head，但旧拆包业务数据允许被清空。

migration 文件必须明确标记“测试阶段破坏性重构”，防止未来被误认为生产无损升级。

## 8. 索引与容量

重点索引：

- unpack_executions(definition_id, started_at)
- unpack_executions(status, updated_at)
- unpack_execution_items(execution_id, status, id)
- unpack_execution_items(execution_id, source_object_key) UNIQUE
- unpack_match_candidates(item_id, generation, score)
- unpack_match_candidates(item_id, generation, site_id, candidate_key) UNIQUE
- unpack_definition_selected_sources(definition_id, source_object_key) UNIQUE

候选列表可能增长。旧 generation 不在普通 UI 默认读取范围，后续可按终态 execution 设计 retention 清理。
