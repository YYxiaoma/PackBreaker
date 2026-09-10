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
- Transmission 请求永远不携带跳过校验语义。

### 5.3 路径与清理

- 恶意 torrent 路径不能越过目标根目录或利用符号链接逃逸。
- 目标冲突不覆盖、不截断、不重命名用户文件。
- 回滚只删除 operation journal 登记且 after snapshot 仍一致的资源。
- `remove_torrent` 的底层下载器调用必须断言为“不删除数据”，并确认公共适配器不存在删除数据参数。

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
- 硬链接隔离副本完成后、替换前后。
- 回滚每个动作前后。

重启对账必须收敛为正确继续、NOOP、完整回滚或 `RECONCILE_REQUIRED`，不能重复产生副作用。

## 7. 适配器测试矩阵

每个站点适配器覆盖连接、认证失败、分页、空结果、详情缺字段、取种、限流、超时、HTML/API 结构变化、熔断与恢复。

每个下载器适配器覆盖版本/能力探测、路径映射、完成任务、暂停添加、重复添加、完整校验、校验失败、连接中断、任务被外部删除和保存路径变化。

所有适配器额外执行 secret canary 测试：在凭证中放入唯一标记，断言日志、异常、repr、缓存键、数据库普通字段和 API 响应中不存在该标记。

## 8. API 与前端测试

- 首次 setup 只能执行一次；会话过期、注销、登录限速和 CSRF 均有效。
- API Token 范围不足返回 403；明文 Token 只在创建响应出现一次。
- Idempotency-Key 同请求重放返回原结果，不同请求返回 409。
- If-Match 冲突返回 412，不覆盖新配置。
- 任务页面正确展示状态、证据、风险和失败原因；状态不只靠颜色表达。真实预演聚合必须优先标记 stale，且只使用进入深度验证的非硬拒绝候选判定 FULL/CLIENT/BLOCKED，不能让高分但已硬拒绝的候选覆盖安全结论。
- 人工审核 revision 必须验证 `expected_version` 并只追加；空 revision、stale preflight、跨 snapshot 候选、硬冲突批准和非 AMBIGUOUS/非候选源文件映射全部失败关闭。首个有效审核只能以 `REVIEW_OPENED` 从 `PREFLIGHT` 进入 `AWAITING_CONFIRMATION`，随后 revision 不得继续改变 task version，且该唯一 bridge 后 preflight 仍应 current。
- 真实任务分析面板只对用户显式输入的后端 task ID 发请求；`PB-*` 演示任务不得自动映射为真实任务。前端 analyze 只提交 `/data` 相对 `source_root`，并正确展示 Unit、Candidate、Preflight current/stale 与稳定错误码。
- 凭证读取始终脱敏，浏览器 URL、store 和 console 中不出现秘密。
- 危险操作显示影响范围，预演过期后不能使用旧确认继续执行。
- 桌面与移动视口完成核心任务、人工确认、路径诊断和日志筛选流程。

## 9. 数据库与迁移

- 新数据库可迁移到 head，重复启动无额外变化。
- 从上一发布版本的匿名化数据库副本升级成功，并保持枚举、外键和幂等唯一性。
- 迁移失败时 worker 不启动，原数据库和一致性备份可恢复。
- WAL 并发测试覆盖 API 读取、任务短写事务和事件追加，不出现长事务锁死。

## 10. 性能与资源测试

M2 使用合成 1 万文件 torrent 和跨文件 piece 测试内存上界与流式解析；M3 使用接近真实大包的目录规模测试扫描和验证。记录磁盘类型、吞吐、CPU、峰值内存和取消响应时间。

在取得真实硬件和语料前不承诺固定吞吐指标。发布门禁要求：处理过程内存不随媒体总字节线性增长；取消能在当前原子块完成后停止；API 和 UI 在后台验证时仍可响应。

## 11. CI 门禁

当前 GitHub Actions 每个 Pull Request 执行三条主门禁，并在 `quality` 最前执行独立仓库安全扫描：

1. `quality`：Python 3.11 + `uv sync --frozen --all-groups`，运行统一静态检查、OpenAPI/生成类型漂移检查、pytest、Vitest 与 production build。
2. `browser-e2e`：安装 Playwright bundled Chromium，启动本地 Vite；认证状态只使用合成 `/auth/me`，不需要真实后端或凭证。
3. `container`：构建三阶段 runtime 镜像，以临时空 `/config`、`/data` 启动，验证 readiness、同源前端首页和镜像默认非 root 用户。

`scripts/repository_scan.py` 扫描 Git 已跟踪文件以及未被 `.gitignore` 排除的工作区候选，阻断真实 `.torrent`、媒体、数据库/日志/密钥类制品、明显私钥/常见 Token 形态以及超过 5 MiB 的单个候选文件；该扫描也被 `scripts/check.py` 本地入口复用。普通 CI 永不连接真实 PT 或下载器。任何安全不变量、迁移、契约、仓库扫描、容器 smoke 或端到端测试失败都应阻止合并。

M1 的逐项退出证据见 [`m1-exit-checklist.md`](./m1-exit-checklist.md)。

## 12. v1.0 验收清单

- 真实验收语料中的自动误辅种为 0。
- qB 与 TR 各完成一条真实端到端任务，包含人工确认和失败回滚。
- 7 个失败样例完成归因并形成回归测试，其中至少覆盖 3 类失败。
- 重复触发 10 次结果唯一。
- LINKING、ADDING、CLIENT_VERIFYING 故障注入后恢复正确。
- 源文件在所有验收场景中内容与 inode 不变。
- 数据库、配置导出、日志、通知和诊断包无可用明文凭证。
- 备份、迁移、升级健康检查和失败回滚演练通过。
