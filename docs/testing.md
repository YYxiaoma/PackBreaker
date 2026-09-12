# 测试与验收计划

## 1. 目标

测试优先证明三项发布目标：不会误辅种、不会损坏源数据、异常后能够恢复。覆盖率用于发现盲区，不能替代对安全不变量和故障场景的明确断言。

默认测试环境完全离线，不访问真实 PT 站点、不操作真实下载器、不读取真实媒体目录。需要真实环境的验收测试必须显式启用并使用专用测试分类、目录和账号策略。

## 2. 测试分层

| 层级 | 范围 | 外部依赖 | 执行时机 |
| --- | --- | --- | --- |
| 单元测试 | 解析、token、评分、映射、状态机、路径、piece、脱敏 | 无 | 每次提交 |
| 属性/模糊测试 | bencode、路径、Unicode、文件边界、状态事件序列 | 无 | 每次提交/夜间 |
| 适配器契约 | 站点、qB/TR、通知的共享行为 | 本地模拟服务 | 每次提交 |
| Repository 集成 | SQLite WAL、迁移、事务、幂等与锁 | 临时 SQLite | 每次提交 |
| 流程集成 | 从触发到预演/添加/回滚 | fake adapters + 临时文件系统 | 每次提交 |
| 前端组件 | 表单、状态、风险提示、脱敏 | Mock API | 每次提交 |
| 端到端 | UI + API + worker + 模拟外部服务 | Docker Compose | 合并前 |
| 真实验收 | 指定 qB/TR 和脱敏真实语料 | 隔离环境 | 里程碑/M6 |

## 3. 测试工具链

- 后端：pytest、pytest-asyncio、Hypothesis、coverage.py、httpx/ASGI transport。
- 前端：Vitest、Vue Test Utils、MSW、Playwright。
- 质量：Ruff 格式与静态检查、mypy 严格模式、TypeScript strict、ESLint。
- 外部协议：本地 HTTP/RPC 模拟服务；响应夹具必须合成或彻底脱敏。

总体行和分支覆盖率目标为 85%。路径安全、验证等级、状态转换、幂等、回滚和凭证脱敏的每个安全分支必须有明确测试，不以总体覆盖率豁免。

站点适配器共享只读契约固定验证 `capabilities/test_connection/search/fetch_details/fetch_torrent`。M-Team 默认测试只使用 `httpx` MockTransport 与合成响应，断言 API Key 仅发送给 API origin、下载第二跳不携带凭证、任意站外下载 URL 被拒绝、torrent payload 大小有界且错误不会回显远端 message/凭证。普通测试不得访问真实 PT 账号。

## 4. 合成语料

测试运行时生成 torrent 和媒体字节，不把 `.torrent` 或媒体文件提交到仓库。生成器应支持固定随机种子，以便失败可重现。

语料矩阵至少包含：

- v1 单文件、多文件和跨文件边界 piece。
- v2 文件树、小文件、Merkle padding 和 piece layers。
- hybrid 合法与 v1/v2 自相矛盾案例。
- padding 文件、零长度文件、缺失 NFO/图片/字幕。
- 相同内容不同目录名、相同 basename 多文件、大小相同内容不同。
- Unicode NFC/NFD、大小写差异、超长路径、绝对路径、`..`、NUL 表示和符号链接。
- 源文件在扫描、验证和执行之间变化。
- 同一媒体的不同剪辑、帧率、音轨、字幕、REMUX/Encode 和多版本。

每个生成案例同时给出期望映射、piece 结果、验证等级和允许动作，避免测试复制实现逻辑。

## 5. 安全不变量测试

### 5.1 源数据不可变

在成功、piece 不匹配、空间不足、权限失败、取消、进程中断和修复场景前后记录源文件：内容 hash、device、inode、size 和 mtime。除 link count 允许因创建/删除硬链接变化外，其余值必须保持不变。

写入修复测试必须证明目标在恢复下载前已成为独立 inode，并且写入目标不会改变源 hash。

### 5.2 校验门

- 只有所有声明 piece 通过才能得到 FULL_VERIFIED。
- 抽样通过、大小相同、名称相同、Info-hash 证据缓存过期都不能单独触发 skip checking。
- 任一 missing/ambiguous 范围、协议不支持或源快照变化均阻止 FULL_VERIFIED。
- Transmission 请求永远不携带跳过校验语义；添加必须显式 `paused=true`，校验只能通过独立 `torrent_verify` 动作触发。
- Transmission 4.1.x 使用 JSON-RPC 2.0 snake_case 协议；测试覆盖 409 session-id 重试、`torrent_add` 重复结果、`torrent_get` 状态/进度解析，以及 stop/verify/start 动作。
- Transmission 做种启动必须绑定同一 execution plan 的 APPLIED `TRANSMISSION_ADD` 与 `TRANSMISSION_VERIFY` journal；启动前重新确认 hash、save path、ownership label、停止状态与 `percent_done=1`，错误/缺失校验证据必须在 `torrent_start` 前失败关闭。
- `TRANSMISSION_START` 覆盖十路并发幂等、响应丢失恢复和真实状态二次确认；status 5 queued-seed 与 status 6 seeding 在 `percent_done=1` 时都视为有效做种态，其他状态不得把任务推进 `DONE`。
- Transmission execution plan 即使面对 `FULL_VERIFIED` 也必须要求 force verify + verify progress 能力；ADDING 固定进入 `CLIENT_VERIFYING`，不得因已有 piece 证据直接跳到做种。
- `TRANSMISSION_ADD` 与 `TRANSMISSION_VERIFY` 必须各自先提交 operation journal intent；相同 candidate/downloader 10 路并发只允许一个远端 add/verify 动作，响应丢失不得盲目重发。
- Transmission 已存在或返回 duplicate 的 torrent 不能仅凭 info-hash 认领；恢复必须同时证明 PackBreaker ownership label、hash 与 save path。verify 未知结果只有 checking 或相对 before snapshot 的可证明变化才能收敛为 APPLIED。
- 本阶段 Transmission 校验成功后停靠 `SEEDING`；在独立 start/做种确认切片完成前，启动恢复和周期驱动必须保持 WAITING，且不得调用 qBittorrent seeding/start 端口。

### 5.3 路径与清理

- 恶意 torrent 路径不能越过目标根目录或利用符号链接逃逸。
- 目标冲突不覆盖、不截断、不重命名用户文件。
- 回滚只删除 operation journal 登记且 after snapshot 仍一致的资源。
- `remove_torrent` 的底层下载器调用必须断言为“不删除数据”；qB 固定 `deleteFiles=false`，Transmission 固定 `delete_local_data=false`，公共适配器不得暴露可切换为删除数据的参数。

### 5.4 Preflight 当前性与不可变性

- 相同 task version、处理单元、源 inventory、启用站点版本和候选证据重复分析，`snapshot_digest` 必须稳定。
- `created_at` 不参与 digest；相同 digest 的重复分析不得写入第二条 preflight 记录。
- 源文件 device/inode/size/mtime、启用站点集合/version 或 task version 在分析期间变化时，必须在持久化前返回冲突并保持零新增 snapshot。
- 单个站点搜索失败只能留下站点级错误证据，不得抹掉其他站点的安全验证结果。
- 声明最小请求间隔的站点单次分析最多执行一条查询；没有已知速率限制的 fake/profile 才可在同一分析中执行逐步放宽查询。

## 6. 幂等与故障注入

相同触发事件并发或连续执行 10 次，断言只存在一个有效任务、一个候选执行、一个最终链接集合和一个下载器任务。

在下列边界注入“动作成功但数据库未确认”和“记录 intent 后动作未执行”两类崩溃：

- 创建目录、创建临时链接、原子重命名之后。
- 下载器添加请求发送前、响应丢失后。
- 客户端校验开始后、状态回写前。
- 下载器做种启动后、START journal 或任务 DONE 状态回写前。
- 下载器任务移除后、REMOVE journal 或文件系统回滚状态回写前。
- 硬链接隔离副本完成后、替换前后。
- 回滚每个动作前后。

重启对账必须收敛为正确继续、NOOP、完整回滚或 `RECONCILE_REQUIRED`，不能重复产生副作用。

operation journal 自身必须先通过状态机/CAS 测试：非法跨级转换失败关闭；APPLIED 必须携带 after snapshot；旧 expected status 不能覆盖并发推进；终态 NOOP/ROLLED_BACK 不进入恢复扫描。真实文件系统动作接入前先用这些契约验证崩溃边界。

文件系统事务链测试必须使用临时数据根，不触碰生产 `/data`：覆盖正常目录+hardlink 创建与十次幂等重放、临时 hardlink 后崩溃并安全续跑、最终 hardlink 落位后但 APPLIED 前崩溃转 `RECONCILE_REQUIRED`、目录创建后但 APPLIED 前崩溃不自动认领、逆序回滚，以及目标被外部替换时 `ROLLBACK_BLOCKED` 且替换文件不被删除。所有场景都复核源 inode/size/mtime 不变，允许 link count 只因预期 hardlink 创建/删除发生变化。

qB 写事务测试使用 fake/MockTransport，不访问真实下载器：覆盖 WebAPI 2.15.1 JSON 添加响应、multipart 中强制 paused、FULL_VERIFIED/skip-check 双层门、WebAPI 2.16 参数失效保护、5.x `stop/start/recheck` 端点、`torrents/info` 的 hash/save path/tag/state/严格 0..1 progress 二次确认、十次顺序与十路并发重放都只发一次 add/recheck、add 响应丢失后凭 hash+save path+ownership tag 恢复、recheck 响应丢失时仅凭 checking 或相对 before snapshot 的可证明变化恢复、状态未变化时转 `RECONCILE_REQUIRED` 而不盲目重发、HTTP 成功但 torrent 不可见、添加前已存在同 hash 不自动认领、save path 不符进入 `RECONCILE_REQUIRED`，以及 paused 被客户端忽略时先 stop 并重新确认后才能 APPLIED。

Transmission 写事务测试同样只使用 fake/MockTransport：除 ADD/VERIFY/START 的 ownership label、hash、save path、完整度和响应丢失恢复外，REMOVE 必须把 `delete_local_data=false` 冻结进 intent 与 RPC 参数；活跃 torrent 先 stop，再确认不存在后才 APPLIED。十路并发 remove 只能发送一次远端删除；响应丢失只能凭 torrent 已消失恢复；ownership label 或保存路径漂移必须在远端删除前阻断。任务取消测试必须证明 Transmission remove 先于 journal-owned hardlink 回滚，remove 成功后崩溃并重启不得二次 remove，且旧 qB cancellation v1 checkpoint 仍能安全恢复。Transmission operation journal 对账必须是严格只读：ADD/VERIFY/START 只有在历史 after snapshot、下载器版本、hash、save path、ownership label 与当前状态共同满足各自后置条件时才能从 `RECONCILE_REQUIRED` 恢复为 APPLIED；REMOVE 还必须由历史 before snapshot 证明原 torrent 归属，并以当前 hash 完全不存在作为后置条件。对账期间任何 add/stop/verify/start/remove 写方法被调用都应直接令测试失败，torrent 再出现、所有权/路径漂移、绑定版本变化或缺失 after snapshot 都必须保持安全阻断。

清理/对账报告必须作为独立只读安全面测试：`RECONCILE_REQUIRED` 与 `ROLLBACK_BLOCKED` 计入人工修复清单，只有已存在安全只读对账器且具备 after snapshot 的项目才能标记 `RECONCILE`；`ROLLBACK_BLOCKED` 和无法证明的未知结果必须始终要求人工检查。保留期候选只能来自 `NOOP` / `ROLLED_BACK`，且候选不等于删除授权。API 响应只能包含固定 OperationKind/状态、task/journal ID、时间和固定原因/建议，测试需在 target/intent/before/after snapshot 中注入绝对路径、ownership tag、幂等键等 canary 并断言响应完全不泄露；报告 endpoint 不得提供 journal/resource 删除或强制状态修改方法。

LINKING 协调器额外覆盖两阶段当前性：调用前 plan provider 必须返回 latest/current/ready；真正推进任务前还要在数据库事务内重新核对 latest plan/gate/review/candidate/preflight/task version。测试必须模拟两次检查之间新增 review revision 并证明零文件副作用；还要模拟 LINKING checkpoint 已提交后 source inventory 改变，证明在首个 operation journal intent/目录/hardlink 前阻断。重复调用已进入 LINKING 的任务只能恢复 checkpoint 精确绑定的同一 plan。

99% 修复安全基础层必须作为纯只读能力测试：v1 mismatch piece 的 `covered_files` 要能识别真实跨文件边界，padding/零长度范围不能制造伪跨文件风险；v2 piece 始终保持文件内作用域。自动 piece 模式在目标仍与源共享 inode、目标 `link_count > 1`、下载器未暂停或隔离/缺失文件预算超过可用空间时不得进入 ready；FILE_ONLY 还必须额外拒绝跨文件 piece 和任何需要隔离的目标。缺失 NFO/图片/字幕只能规划为目标侧完整文件获取，不允许产生源目录写入。`inspect_repair_target` 测试要证明 hardlink/独立 inode/missing target/symlink/source snapshot 漂移均被正确区分且检查前后没有文件副作用。所有 planner 测试必须断言 `execution_allowed=false`，直到后续 journal-backed copy/fsync/atomic-replace 与 downloader repair executor 完成。

## 7. 适配器测试矩阵

每个站点适配器覆盖连接、认证失败、分页、空结果、详情缺字段、取种、限流、超时、HTML/API 结构变化、熔断与恢复。

站点可靠性层使用 fake clock/sleep 与 `httpx` MockTransport 单独验证：同一 search key 并发 miss 只能产生一次真实请求，后续命中 TTL 缓存；不同 key 按 `min_request_interval_seconds` 排队；429/临时失败使用带抖动退避并尊重 `Retry-After`，且总重试调度不得越过 deadline。`SITE_AUTH_FAILED` 必须一次失败立即开路，连续 `SITE_INVALID_RESPONSE` 与临时错误达到阈值后开路；冷却后只允许一个 half-open 探测，取消该探测必须释放占位，成功才恢复闭路。站点 config version 变化必须清空旧缓存/熔断状态。`fetch_torrent` 不缓存且不自动重试一次性令牌流程，确保 execution plan/reverify/ADDING 每次仍用新鲜 metainfo digest 做安全复核。缓存键、异常与 `repr` 不得包含 credential canary。
health 还必须验证 circuit CLOSED/OPEN/HALF_OPEN、retry-after、限流等待、cache hit/miss/eviction、真实请求与重试计数均与 fake clock/调用次数一致；人工 `reset_circuit` 保留累计指标且不产生虚假的 recovery 通知。health/API 响应不得包含 base URL、凭证或第三方正文。管理员 reset 需要 CSRF + `If-Match`，`config:write` Token 可无 CSRF 调用而 `config:read` 只能读取 health。熔断打开/恢复通过通用 notification outbox 的 `SITE` subject 聚合，相同站点+事件键复用同一 outbox；非法 error code 必须退化为固定安全码。`0014 → 0015` 迁移必须把已有 task outbox 回填为 `TASK` subject 且保持 task/event 外键证据。


每个下载器适配器覆盖版本/能力探测、路径映射、完成任务、暂停添加、重复添加、完整校验、校验失败、连接中断、任务被外部删除和保存路径变化。

所有适配器额外执行 secret canary 测试：在凭证中放入唯一标记，断言日志、异常、repr、缓存键、数据库普通字段和 API 响应中不存在该标记。

通知适配器只使用 `httpx` MockTransport 做默认测试：断言 Telegram 固定调用官方 Bot API `sendMessage`、Server酱按 SendKey 类型选择固定官方端点、禁止重定向，远端 401/403/429/5xx/非法 JSON 不回显 token/SendKey/响应正文。TaskEvent 与 outbox 必须同事务回滚；相同 TASK/SITE subject + 事件键重复事件只形成一条滚动 outbox，首次发送后窗口内聚合；发送失败仅推进 outbox RETRY/DEAD，不改变 task 或 site 状态。NotificationDriver 需要覆盖重入保护和 shutdown 取消。

## 8. API 与前端测试

- 首次 setup 只能执行一次；会话过期、注销、登录限速和 CSRF 均有效。
- API Token 范围不足返回 403；明文 Token 只在创建响应出现一次。
- Idempotency-Key 同请求重放返回原结果，不同请求返回 409。
- If-Match 冲突返回 412，不覆盖新配置。
- 任务页面正确展示状态、证据、风险和失败原因；状态不只靠颜色表达。真实预演聚合必须优先标记 stale，且只使用进入深度验证的非硬拒绝候选判定 FULL/CLIENT/BLOCKED，不能让高分但已硬拒绝的候选覆盖安全结论。
- 人工审核 revision 必须验证 `expected_version` 并只追加；空 revision、stale preflight、跨 snapshot 候选、硬冲突批准和非 AMBIGUOUS/非候选源文件映射全部失败关闭。首个有效审核只能以 `REVIEW_OPENED` 从 `PREFLIGHT` 进入 `AWAITING_CONFIRMATION`，随后 revision 不得继续改变 task version，且该唯一 bridge 后 preflight 仍应 current。
- 真实任务分析面板只对用户显式输入的后端 task ID 发请求；`PB-*` 演示任务不得自动映射为真实任务。前端 analyze 只提交 `/data` 相对 `source_root`，并正确展示 Unit、Candidate、Preflight current/stale 与稳定错误码。
- 凭证读取始终脱敏，浏览器 URL、store 和 console 中不出现秘密。
- 通知渠道 GET 只返回 `credential_configured`；前端编辑默认 `KEEP`，只有显式替换凭证才发送新 secret，关闭/成功/失败后清空凭证输入。创建、更新、删除和启停分别验证 CSRF/scope 与强 `If-Match`。
- 危险操作显示影响范围，预演过期后不能使用旧确认继续执行。
- 手动 Analyze 必须按 `ANALYZING → SEARCHING → MATCHING → VERIFYING → PREFLIGHT` 记录状态事件；失败恢复只能在任务 version 仍由本次运行持有时进入 `RETRY`，不得覆盖并发状态变化。
- 人工映射重验证必须重新校验 preflight/current、source inventory、review revision、torrent 身份和 metainfo digest；重验证结果只追加不可变证据，审核或预演在验证期间变化时不得落库。
- Pre-execution gate 必须绑定当前 task/preflight/review/candidate/重验证证据；FULL_VERIFIED 与 CLIENT_CHECK_REQUIRED 的资格语义必须区分，后者始终携带 `client_check_required=true`。BLOCKED、hard reject、stale 或缺少必要重验证证据时必须失败关闭，且 gate 生成不得启动任何文件系统或下载器副作用。
- Execution plan 必须绑定 current+eligible gate 和相同 metainfo digest，只保存 `/data` 相对源/目标路径；目标树只能只读检查。目标冲突、父目录/符号链接异常和跨设备必须成为稳定 blocker，目标状态变化必须让旧计划 stale；`execution_allowed` 与 `side_effects_started` 在当前阶段始终为 false。
- 桌面与移动视口完成核心任务、人工确认、路径诊断和日志筛选流程。

## 9. 数据库与迁移

- 新数据库可迁移到 head，重复启动无额外变化。
- 从上一发布版本的匿名化数据库副本升级成功，并保持枚举、外键和幂等唯一性。
- 迁移失败时 worker 不启动，原数据库和一致性备份可恢复。
- WAL 并发测试覆盖 API 读取、任务短写事务和事件追加，不出现长事务锁死。

## 10. 性能与资源测试

M2 使用合成 1 万文件 torrent 和跨文件 piece 测试内存上界与流式解析；M3 使用接近真实大包的目录规模测试扫描和验证。记录磁盘类型、吞吐、CPU、峰值内存和取消响应时间。

在取得真实硬件和语料前不承诺固定吞吐指标。发布门禁要求：处理过程内存不随媒体总字节线性增长；取消能在当前原子块完成后停止；API 和 UI 在后台验证时仍可响应。

真实目录绑定到项目容器后，先运行 `python scripts/m2_corpus_acceptance.py <torrent> <source_root>` 做至少 3 轮只读扫描/自动映射稳定性检查。默认不会执行完整媒体 hash，也不会创建目录、硬链接或下载器任务；确认结构和映射合理后，再显式追加 `--verify-content` 执行完整 v1 piece / v2 Merkle / hybrid 双协议内容验证。JSON 输出只保留协议统计、目录/映射摘要与不可逆 digest，不输出 tracker、source 或 Info-hash。

## 11. CI 门禁

当前 GitHub Actions 每个 Pull Request 执行三条主门禁，并在 `quality` 最前执行独立仓库安全扫描：

1. `quality`：Python 3.11 + `uv sync --frozen --all-groups`，运行统一静态检查、OpenAPI/生成类型漂移检查、pytest、Vitest 与 production build。
2. `browser-e2e`：安装 Playwright bundled Chromium，启动本地 Vite；认证状态只使用合成 `/auth/me`，不需要真实后端或凭证。
3. `container`：构建三阶段 runtime 镜像，以临时空 `/config`、`/data` 启动，验证 readiness、同源前端首页和镜像默认非 root 用户。

`scripts/repository_scan.py` 扫描 Git 已跟踪文件以及未被 `.gitignore` 排除的工作区候选，阻断真实 `.torrent`、媒体、数据库/日志/密钥类制品、明显私钥/常见 Token 形态以及超过 5 MiB 的单个候选文件；该扫描也被 `scripts/check.py` 本地入口复用。普通 CI 永不连接真实 PT 或下载器。任何安全不变量、迁移、契约、仓库扫描、容器 smoke 或端到端测试失败都应阻止合并。

M1 的逐项退出证据见 [`m1-exit-checklist.md`](./m1-exit-checklist.md)。
M2 的代码能力、自动化证据与仍依赖真实语料/环境的退出项见 [`m2-exit-checklist.md`](./m2-exit-checklist.md)。

## 12. v1.0 验收清单

- 真实验收语料中的自动误辅种为 0。
- qB 与 TR 各完成一条真实端到端任务，包含人工确认和失败回滚。
- 7 个失败样例完成归因并形成回归测试，其中至少覆盖 3 类失败。
- 重复触发 10 次结果唯一。
- LINKING、ADDING、CLIENT_VERIFYING 故障注入后恢复正确。
- execution plan 必须绑定明确的目标下载器 version/能力摘要/远端 save path；下载器配置、能力或路径映射变化后旧计划必须 stale，不能在 ADDING 时临时换客户端。
- ADDING 覆盖 qB 响应丢失与“journal 已 APPLIED、task 状态尚未提交”两个崩溃点；重复执行只能收敛到同一个 qB 任务。FULL_VERIFIED 可按能力进入 SEEDING，CLIENT_CHECK_REQUIRED 必须保持 `skip_checking=false` 并进入 CLIENT_VERIFYING。
- CLIENT_VERIFYING 必须以独立 recheck journal + 单步状态 tick 工作：校验开始后崩溃或响应丢失不得重复 recheck；只有存在 checking/完成变化证据且最终 `progress=1` 才进入 SEEDING，观察过 checking 后以 `<1` 停止则进入 RETRY；外部删除、save path/tag 变化必须失败关闭并要求对账。
- SEEDING 必须以独立 start journal 驱动：start intent 前再次确认 source inventory、target root、下载器 binding、add journal 与可选 recheck journal；只允许停止且 `progress=1` 的本系统 torrent 启动，实际进入 `uploading`/`stalledUP`/`queuedUP`/`forcedUP` 且 `progress=1` 后才能 `SEEDING → DONE`。连续或 10 路并发触发只允许一个有效 start；start 响应丢失和“journal 已 APPLIED、task 尚未 DONE”必须无重复副作用恢复，外部停止、删除、save path/tag 漂移必须失败关闭或进入对账。
- 启动恢复必须有界扫描 `LINKING/ADDING/CLIENT_VERIFYING/SEEDING/ROLLING_BACK`，按最久未更新优先；lifespan 启动调用还可显式扫描遗留 `CANCELLING + COOPERATIVE_ANALYSIS`，但必须同时证明 checkpoint schema/mode/stage、原分析 stage、`analysis_version == task.version - 1`、最近 `CANCELLATION_STARTED` from→to、`remove=false/rollback=false` 与 operation journal 为空，才能收敛到 CANCELLED。资源式/畸形 CANCELLING 或已有 journal 必须 BLOCKED 且状态不变。普通周期 driver 必须保持默认模式并忽略 CANCELLING，避免抢占仍存活的分析协程。同一 stage 未变化时停止本轮，坏 checkpoint/plan 只能阻断对应任务且不能阻断后续任务或 readiness。恢复程序级异常必须让启动失败，同时释放实例锁；limit 截断不得触发未扫描任务的任何副作用。启动后的周期 driver 必须串行复用同一 reconciliation、防止重入；一次 tick 异常不得杀死后续 tick，shutdown 取消正在执行的 tick 后必须依赖 journal 在下次启动恢复。CLIENT_VERIFYING 必须能在不重启应用的情况下由后续周期 tick 收敛。
- 取消/回滚必须证明 qB 与文件资源都属于当前 task/execution plan：qB remove 必须固定 `deleteFiles=false`，响应丢失后不得盲目重复；若 qB 任务仍存在，文件回滚前必须先移除下载器任务。hardlink 与目录只按 journal ID 逆序撤销，外部替换、非空目录或未决文件 journal 必须阻断自动完成。故障注入覆盖“qB remove 已 APPLIED、task 仍 ROLLING_BACK”，启动恢复不得产生第二次 remove。
- 副作用开始前取消必须覆盖 `PENDING/PREFLIGHT/AWAITING_CONFIRMATION/PAUSED/RETRY`：请求只能是 `remove=false/rollback=false`，服务端必须再次证明 task 没有任何 operation journal，随后在同一事务中记录 `CANCELLING → CANCELLED`，且 qB/文件系统 coordinator 调用次数为 0。`ANALYZING/SEARCHING/MATCHING/VERIFYING` 必须采用协作式取消而非抢改终态：cancel 先冻结原分析 stage/version 到 `COOPERATIVE_ANALYSIS` checkpoint 并返回 `CANCELLING`，原分析流在下一安全检查点验证零 journal 后完成 `CANCELLED`；四个分析 stage 都要覆盖进程重启后 startup-only 安全收敛，同时验证普通周期 recovery 不会处理该 CANCELLING。至少覆盖站点 await 中并发取消、source inventory 目录遍历取消检查、v1 piece 批次取消检查；大文件 scan/hash 必须在 worker thread 中执行，避免事件循环无法并发接收 cancel。故障注入覆盖稳定任务已 CANCELLED、receipt 仍 PENDING，以及活动分析已 CANCELLING、receipt 仍 PENDING 两类响应丢失窗口；同一 Idempotency-Key 重放不得调用资源回滚 coordinator。
- 公开 `execute`/`cancel` 必须覆盖管理会话 CSRF 与 API Token `tasks:write`，副作用动作缺少 `Idempotency-Key` 返回 428；键明文不得落库，同 actor/key 同请求只调用一次底层 coordinator，同键不同请求返回 `IDEMPOTENCY_CONFLICT`。可安全归类失败应持久化并重放；未分类异常保持 PENDING。还必须覆盖 receipt PENDING 后 task 已被后台推进到后续 stage 的 execute 重放，确认不会重新进入 LINKING 或倒退状态。
- 浏览器门禁必须覆盖公开动作 UI：execute 首次网络结果未知后冻结原 execution plan ID 与同一 `Idempotency-Key`，即使 SSE 已推进 task 也只能幂等重放原请求；合成 `TaskEvent` SSE 必须实际出现 `QBITTORRENT_SEEDING_CONFIRMED` 后观察 DONE。稳定零副作用取消必须覆盖响应丢失 + 同 key 确认；活动分析取消必须从 SEARCHING 提交 `remove=false/rollback=false`，先观察公开动作返回 CANCELLING，再由 `CANCELLATION_COMPLETED` SSE 观察 CANCELLED。副作用阶段取消 UI 必须阻断 rollback-only，显式勾选 qB remove + journal-owned rollback 后由 `ROLLBACK_COMPLETED` SSE 观察 `ROLLING_BACK → CANCELLED`，全程不得访问真实后端/qB。
- 前端公开动作控制必须覆盖：只有 `READY + CURRENT + AWAITING_CONFIRMATION` 才可执行；执行确认展示目标下载器、hardlink/目录/CLIENT_FETCH 数量、下载上界和客户端校验要求；未知结果重试复用原幂等键。取消默认不选择资源动作，回滚 journal-owned 文件时必须同时选择移除 qB 任务，`CANCELLING/ROLLING_BACK` 禁止重新提交不同选项。
- 任务事件接口必须覆盖历史顺序、`after_event_id` 增量、跨 task 游标拒绝、SSE `Last-Event-ID` 续接、no-cache/no-buffering 与脱敏字段边界。前端优先 EventSource，同源会话 cookie 鉴权；SSE 断线才启用事件增量轮询，不允许把 bearer token 放进 URL 查询参数。
- operation journal → TaskEvent 投影必须与 journal insert/CAS 同事务：幂等 intent 重放不得重复事件；qB 事件不得包含 downloader ID、torrent hash、save path、ownership tag、target/intent/snapshot 原文；未知 operation type 不得回显。大型文件包的 routine directory/hardlink intent/APPLIED/ROLLED_BACK 必须抑制为 LINKING/ROLLBACK 批次计数摘要，仅 `RECONCILE_REQUIRED`/`ROLLBACK_BLOCKED` 逐 journal 暴露固定资源类别。浏览器 SSE 至少观察 qB add/recheck/start/remove 的 APPLIED 摘要。
- operation journal 对账接口必须验证响应字段白名单，禁止泄露 target/intent/before/after snapshot、路径、hash、ownership tag、原始 operation type 或 Idempotency-Key。文件系统 reconcile 只允许带 after snapshot 的 `RECONCILE_REQUIRED`，当前资源精确匹配时才 CAS 回 APPLIED，外部替换保持阻断且零文件写入。qB reconcile 只允许已有历史 after snapshot 的 ADD/RECHECK/START：必须复核 downloader ID/version、hash、save path、ownership tag 与对应后置状态，全程只允许 `get_torrents`，测试断言 add/stop/recheck/start/remove 调用计数不增加；binding version、ownership、save path、状态任一漂移都保持阻断。qB REMOVE、无 after snapshot 的未知结果和 `ROLLBACK_BLOCKED` 不得自动 reconcile。还必须覆盖响应丢失窗口（journal 已 APPLIED、receipt 仍 PENDING）同 key 重放只再次验证证据并补 receipt、缺 key=428、管理会话缺 CSRF=403、`tasks:write` bearer 可调用而 `tasks:read` bearer=403。浏览器门禁必须证明 SSE 已显示 APPLIED 后仍只能复用原 journal + 原 key 重试确认。
- 源文件在所有验收场景中内容与 inode 不变。
- 数据库、配置导出、日志、通知和诊断包无可用明文凭证。
- 备份、迁移、升级健康检查和失败回滚演练通过。
