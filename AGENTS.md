# AGENTS.md — PackBreaker 当前代码库开发准则

本指南适用于 PackBreaker 仓库中的自动化编码代理（包括 GPT-6）。**以当前工作树中的实现为事实来源**，不是产品历史、版本变更日志或功能宣传。目标是在保护媒体、下载器和凭证的前提下，尽可能自主地完成研发、验证和交付。

## 1. Agent 执行方式

1. **先检查，再行动**：确认 Git 分支、工作区改动、用户目标；定位相关模块和测试。以正在运行的代码、接口、状态机及 Registry 为准，不从旧文档推断功能已实现。
2. **主动推进**：对明确授权的工作自行完成调查、编码、测试、修复、复测和 diff 审查。小任务直接做；跨模块任务使用简短计划。避免反复要求用户确认已经给出的信息或许可。
3. **按需读取**：先读相关入口及调用链，再扩大到相邻层；不要例行通读全部 docs 或运行无关的全量测试。保留长任务的当前分支、HEAD、改动和验收证据，避免重复工作。
4. **权限与副作用分级**：普通源码/文档编辑、只读检查及隔离合成测试可以在已授权工作区自主执行。真实站点敏感请求、取种、下载器写 RPC、媒体/辅助文件物化或删除、真实数据库迁移、Docker 守护进程、NAS/宿主机变更、远端推送及发布，必须满足各自的用户授权、程序门禁和执行环境许可。已有许可仅适用于其明确范围，不能替代实际权限。
5. **失败时继续做可做的事**：将代码问题、测试问题、站点故障和权限拦截分别诊断；处理可修复的部分。不得绕过宿主机或工具安全拒绝，也不能通过更换工具、关闭检查或伪造授权达到相同写入效果。
6. **用证据沟通**：区分“已编码”“自动化通过”“真实环境验收通过”“仍待验收/外部阻断”。中文汇报，协议字段和稳定状态码保留英文。不要把 Mock/Fake、构建绿灯或仅有提案代码写成真实业务完成。

## 2. 当前产品结构与功能入口

以下模块来自现有前后端代码；**有入口或适配器不代表所有外部站点均已完成真实验收**。

| 用户功能 | 当前入口 | 主要实现位置 |
| --- | --- | --- |
| 总览 | 系统、磁盘、CPU、内存、站点/下载器连接与任务生命周期 | `frontend/src/components/OverviewDashboard.vue`、`backend/app/application/system_health.py` |
| 数据拆包 | 任务中心：手动/监控拆包、任务筛选、执行记录、影片发现/匹配、候选审核、内容校验、辅种及错误恢复 | `frontend/src/components/TaskCenterV2.vue`、`backend/app/api/unpack.py`、`backend/app/application/unpack_*.py` |
| 数据去重 | 任务中心：目录预检、去重任务、候选对、审核、完整哈希核对与受控执行 | `frontend/src/components/MovieDedupPanel.vue`、`backend/app/api/movie_dedup.py`、`backend/app/application/movie_dedup*.py` |
| 站点管理 | 两卡切换站点配置和 CookieCloud；站点固定 Profile、Cookie/API Key、代理、连接测试、支持状态与站点适配器 | `frontend/src/components/SiteManagement.vue`、`backend/app/api/sites.py`、`backend/app/domain/site_config.py` |
| 下载器 | 系统设置：qBittorrent/Transmission 配置、连接、任务/指标、路径映射及受控动作 | `frontend/src/components/DownloaderManagement.vue`、`backend/app/api/downloaders.py`、`backend/app/application/*downloader*.py` |
| CookieCloud | 站点管理：配置、连通性、手动/计划同步及本地凭证处理 | `frontend/src/components/CookieCloudManagement.vue`、`backend/app/application/cookiecloud*.py` |
| 通知 | 系统设置及站内 Inbox：Telegram、Server酱、连接测试、事件通知/审核 | `frontend/src/components/NotificationManagement.vue`、`backend/app/api/notifications.py` |
| AI 助手 | 系统设置：Provider、模型、只读 Tools、Telegram 对话 | `frontend/src/components/AIAgentManagement.vue`、`backend/app/application/ai_*.py` |
| 系统运维 | 系统设置及版本入口：日志、健康/诊断、备份恢复、版本检查与受控在线升级 | `frontend/src/components/OperationalLogs.vue`、`frontend/src/components/BackupManagement.vue`、`frontend/src/components/VersionPopover.vue`、`backend/app/api/system.py` |
| 管理与外观 | 管理员登录/改密、用户菜单、明暗主题、响应式导航 | `frontend/src/App.vue`、`backend/app/api/auth.py` |

### 数据拆包的主业务链

按实际代码追踪 `unpack_definitions`、`unpack_discovery`、`unpack_matching`、`unpack_content_verification`、`unpack_execution_plans`、`unpack_materialization`、`unpack_seeding`、`unpack_item_actions`、`unpack_monitor` 等模块。核心语义是：

`任务定义/来源 → 影片发现 → 跨站候选 → 详情与 torrent 元信息 → 内容验证 → 人工审核/安全门 → 冻结执行计划 → 文件复用或受控物化 → 客户端校验 → 启动做种 → 记录/恢复/对账`

- 手动与监控任务共用业务执行链，但触发、筛选及调度方式不同。
- 影片项、任务定义、执行实例、下载器操作各有生命周期；修改重试/状态机时同时检查 API、数据库、前端和后台调度。
- 匹配失败、网络超时、辅助文件补齐、客户端离线与重启恢复应有明确的错误分类、重试上限、幂等语义和人工介入出口。
- 现有 scoped acceptance 的提案、证据、审计、宿主机检查模块并不自动授予写入权限；**只读冻结实例不可升级为写入实例**。以 worker 的实际门禁、事务条件及真实环境授权为准。

## 3. 代码组织与修改落点

```text
backend/app/api/                    HTTP API、权限校验及输入输出契约
backend/app/application/            拆包/去重/站点/下载器/通知/AI 等应用流程
backend/app/domain/                 状态机、安全门、验证与业务不变量
backend/app/infrastructure/         外部适配器、文件与持久化实现
backend/migrations/                 Alembic 数据库迁移
frontend/src/components/           管理界面与交互
frontend/src/api/                  API 客户端及生成类型
frontend/src/stores/               前端状态
tests/{unit,api,application,contract,integration}/  分层回归
scripts/                            检查、测试、OpenAPI、验收与发布辅助
.github/workflows/                  CI、候选镜像和正式发布流水线
```

- **领域优先**：安全判定与状态迁移放在 domain/application 层；API router 不自行复制业务门禁；适配器保留 qB、Transmission、PT 站点的协议差异。
- **完整跨层修改**：API 变更应同步 `frontend/openapi.json`、`frontend/src/api/generated/schema.ts`、调用处、契约/回归测试；数据库字段变更应同步 ORM、Alembic、序列化与迁移测试。
- **界面以当前组件为准**：检查桌面和窄屏、亮/暗主题、loading/empty/error、按钮可点击区域、长内容省略和弹窗状态。不要依照早期菜单截图或已删除的路由重新引入功能。
- **文档按需参考**：模块复杂时再阅读 `docs/architecture.md`、`docs/matching-and-filesystem-safety.md`、`docs/adapters.md`、`docs/testing.md` 和当前相关验收文档。文档与运行代码不符时先核实并显式记录差异，不能让旧版设计覆盖当前实现。

## 4. 不可削弱的系统安全边界

### 媒体与文件系统

- 源媒体默认只读：不自动删除、覆盖、截断、改名或修改源 inode。Hardlink 目标若可能被下载器写入，必须先复制/隔离为独立 inode。
- 文件副作用需使用显式授权的挂载/目标根、规范化路径和源/目标快照；拒绝路径穿越、NUL、符号链接逃逸、未证明归属的覆盖及跨设备硬链接。容器根 `/` 不等于写入授权根。
- 数据去重只能在完整内容哈希及身份/设备/inode 校验等条件成立时执行；预检、人工审批、原子变更和异常恢复不可省略。
- 任何文件创建、链接、修复、删除或回滚只针对具有 journal/快照归属证据的对象。不能擅自处理已有用户媒体或种子以凑齐验收。

### 匹配、站点与下载器

- 文件名、大小、媒体 ID、相似度及抽样校验只用于候选筛选；不能替代 torrent piece/内容验证。只有符合领域要求的 `FULL_VERIFIED` 才可能走免除部分客户端校验的路径。
- qBittorrent 的跳校验还依赖实际 API 能力；Transmission 的 verify 必须独立执行。客户端行为须核对 hash、保存路径、归属和真实状态，不得仅凭请求成功认为已做种。
- 站点支持状态以 `SiteProfileRegistry` 和后端启用逻辑为准。`PENDING_ADAPTER`、`PENDING_REAL_VALIDATION` 不能伪装成 `SUPPORTED`；只读连接测试、受限验收和全局正式任务授权是不同权限。
- 站点 Cookie/API Key 只能发送到审查过的固定源站；不尝试绕过验证码/反爬，不对一次性取种 token 盲目重试。
- 下载器与文件系统写操作遵循 **preflight → 冻结计划与快照 → journal intent → 副作用 → journal 结果 → 对账/恢复**；丢响应先读真实状态，不能重复执行可能非幂等的动作。删除客户端任务时不得连带删除本地数据。
- 受限验收的只读 execution 必须永久保持只读；后续可写验收必须使用新的执行实例、独立的有效许可、精确目标及逐操作的实时准入，不可将离线提案、风险报告或宿主证据签名视为执行许可。

### 账户、秘密、AI 与审计

- Cookie、Passkey、API Key、登录密码、下载器凭证、Telegram Token、announce 参数等进入受保护的 SecretStore/哈希机制，不得出现在源码、Git、日志、错误详情、测试夹具、诊断导出、通知或 AI Tool 上下文。
- 保留管理员认证、强制改密、会话撤销、CSRF、幂等键、人工审批、审核日志及 Telegram allowlist。不能通过前端隐藏代替服务端验证。
- 内置 AI 助手保持受控只读 Tools 与最小权限，不开放任意 shell、SQL、外部 URL 或绕过执行门禁的写入工具。
- 数据库存储/时间/备份需与现有 SQLite WAL、Alembic 和 SecretStore 兼容；真实配置或数据库验收优先使用一致性备份副本，不对生产库直接运行开发迁移。

## 5. 验证与交付

- 修 Bug 先尝试最小复现，并补回归测试；新功能覆盖正常路径、无权限/无凭证、冲突、超时、失联、重复执行和恢复等相关场景。
- 先做最小相关测试，再根据受影响范围运行仓库门禁。常用命令：
  - `python scripts/check.py`：仓库扫描、证据/基线检查、Ruff、mypy、前端 lint/typecheck 和 OpenAPI 漂移检查。
  - `python scripts/test.py`：pytest、Vitest 与前端构建。
  - 前端交互与拆包界面变更视情况运行 `node scripts/check-prototype.cjs`、`node scripts/check-unpack-v2-ui.cjs` 和相关浏览器 E2E。
- 普通自动化只用隔离合成数据，默认不连接真实站点、下载器、媒体路径、生产 Bot、AI Provider 或 Docker daemon。需要外部环境验证时先明确请求和潜在副作用；未验收不能写“已通过”。
- 版本与发布身份从当前 `pyproject.toml`、前端包、Git tag、实际工作流及不可变镜像摘要核实；`release-baseline.json` 保持上一正式发布基线，不能为了候选版本一致性而篡改。
- 提交前检查 `git diff`、测试输出、文件列表和敏感信息。不得提交真实 `.torrent`、数据库、媒体、密钥、运行日志或缓存。未经对应授权，不执行 commit、push、merge、tag、部署或正式发布。
- 交付说明只报告：**实际修改文件与行为、运行的验证及结果、尚未完成的真实验收/阻断、Git/发布状态**。不虚构完成度，不通过弱化安全断言获得绿灯。

**维护原则**：本文件仅描述当前架构的稳定事实、操作入口和必须保留的约束。不编入历代版本的发布历史、已废弃菜单、过时的功能矩阵或某一轮的临时验收数字；功能调整时以最新代码为主同步更新对应部分。
