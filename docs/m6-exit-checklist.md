# M6 发布与运维闭环退出清单

## 1. 当前状态

M6 于 2026-09-14 在 M5 正式关闭后启动。M6 不放宽既有安全门；真实 qBittorrent、Transmission、PT 站点和媒体目录继续只在显式现场验收中使用，普通自动化测试保持离线与合成数据。

## 2. 发布与运维工作包

| 工作包 | 状态 | 当前证据/缺口 |
| --- | --- | --- |
| 一致性数据库备份 | ✅ CI 闭环 | SQLite Backup API、SHA-256 manifest、完整性/revision 验证、CLI、preview-first 保留、默认关闭的计划调度、强 `If-Match` 管理 API 与真实 UI 已接通；GitHub Actions run `34851129257` 已在 linux/amd64 Docker 环境完成真实备份与离线 verify/restore 演练 |
| 恢复与失败回滚 | ✅ CI 闭环 | 已实现实例锁停止门禁、恢复前安全快照、临时迁移预检、原子切换与切换后失败自动回滚；GitHub Actions run `34851129257` 已在 linux/amd64 Docker 环境完成空配置启动、preflight、备份、离线 verify/restore 与重启 readiness |
| 升级兼容矩阵 | ✅ 跨版本门禁闭环 | 已自动覆盖 22 个历史 Alembic revision（0001～0022）→ `0023_backup_policy`、空配置原子首装、迁移失败不切换和 Runtime 启动安全升级；正式 `v0.1.1` Release workflow run `34924614659` 已真实跑绿 `v0.1.0 → v0.1.1 → 恢复 v0.1.0`，当前 `release-baseline.json` 已推进到 `v0.1.1` |
| 真实依赖健康/仪表盘 | ✅ 代码闭环 | `/system/health` 已聚合 runtime、磁盘、备份、任务/operation 风险、站点/下载器既有证据、通知与后台 driver，前端总览直接消费 typed OpenAPI；读取页面不主动访问外部服务 |
| 日志与诊断导出 | ✅ 代码闭环 | stdout JSON + `/config/logs` 有界轮转日志、7 天最大查询窗口、查询/导出条数硬限制、typed API 与前端筛选/导出已接通；诊断 ZIP 继续独立且默认不含日志，日志/诊断均有泄漏 canary |
| 镜像/SBOM/发布产物 | ✅ 发布闭环 | `v0.1.1` Release workflow run `34924614659` 已真实发布 linux/amd64 GHCR 镜像、SPDX JSON SBOM、release manifest、SHA256SUMS 与 GitHub Release；公开 `0.1.1`、`stable` 和 manifest 均指向 `sha256:76f4c041d1acecbb573cdbd49c45aec936bfd8cf3b263bc09153f7741f18e30d` |
| 运维 runbook/用户手册 | ✅ 闭环 | 已补 Compose 备份/停止/恢复/readiness/回滚步骤；`0.1.2` 候选新增独立 updater helper 的 Web 一键升级、安全容器重建、healthcheck 与失败自动回滚；无 helper 或复杂拓扑仍保留手工 digest runbook |
| v1.0 全量验收 | ✅ 验收闭环 | 25 条验收项已建立机器可校验证据索引：21 条自动化/CI 覆盖、4 条现场证据、0 条 `pending_external`；首个正式 tag 的 registry/SBOM/Release 供应链证据也已完成 |

## 3. 一致性备份首批能力

- `python -m backend.app.maintenance backup` 默认把数据库快照写入 `<PACKBREAKER_CONFIG_DIR>/backups`。
- 快照使用 Python `sqlite3.Connection.backup()`，允许源数据库处于 WAL 模式且应用仍在运行；不采用直接复制 `packbreaker.db` 的方式。
- 每个快照生成同名 `.json` manifest，记录格式版本、UTC 创建时间、应用版本、数据库 SHA-256、字节数和 Alembic revision。
- 数据库和 manifest 均以 0600 权限创建，备份目录收紧为 0700；目标目录拒绝符号链接。
- `python -m backend.app.maintenance verify-backup <backup.db>` 会重新验证摘要、大小、`PRAGMA integrity_check` 和 Alembic revision。
- manifest 不记录源数据库绝对路径，也不会复制 `secret.key`。数据库内已有凭证仍保持应用层密文；主密钥必须独立备份。

## 4. 离线恢复首批能力

- `python -m backend.app.maintenance restore-backup <backup.db> --confirm-replace-current-database` 执行离线恢复。
- 恢复先获取与主服务相同的 `/config` 实例锁；活动 PackBreaker 实例仍在运行时直接失败关闭，不会切换数据库。
- 当前数据库存在时，恢复前自动使用同一一致性备份格式写入 `backups/pre-restore` 安全快照。
- 待恢复数据库先复制到同文件系统临时文件，执行 Alembic `upgrade head` 迁移预检、`integrity_check` 与 journal 规范化，通过后才原子替换当前数据库。
- 原子替换后再次验证 migration revision；若此阶段失败，自动从恢复前安全快照回滚。自动化测试覆盖活动实例阻断、正常恢复和切换后失败回滚。

## 5. 发布 preflight 与备份保留

- `python -m backend.app.maintenance preflight` 只检查本地 `/config`、SQLite migration/integrity、现有主密钥、`/data` 根目录和 docker.sock 风险；不连接 PT/下载器/通知，不遍历媒体树。
- preflight 默认在 `/config/backups/preflight` 创建并立即复验一份一致性临时快照，随后只清理本次创建的 `.db/.json`；可用 `--skip-backup-exercise` 显式跳过，但报告保留 warning。
- `python -m backend.app.maintenance backup-retention --retention-days 30 --keep-latest 3` 默认只生成 preview。只有追加 `--apply --confirm-delete-expired-backups` 才执行删除。
- 保留策略只识别备份根目录中命名、manifest、摘要与 SQLite 完整性都有效的 PackBreaker 标准成对备份；`pre-restore/` 不递归扫描，外来文件忽略，孤儿/损坏 pair 标记 blocked，执行前还重新校验 inode 身份。

## 6. 升级兼容与发布供应链

- `tests/integration/test_upgrade_matrix.py` 参数化覆盖 `0001_m1_core` 到 `0022_history_scan_cancelled` 全部 22 个历史 revision 升级到当前 `0023_backup_policy`，并证明业务探针保留、升级前安全快照 revision 正确。
- Runtime 启动取得实例锁后，对旧数据库先创建 `backups/pre-upgrade` 一致性快照，再只迁移同文件系统临时副本；迁移失败不切换当前数据库，切换后验证失败恢复安全快照。空配置首次安装也先迁移临时数据库，通过后才原子安装，失败不遗留半成品目标数据库。
- Dockerfile 的 Node/Python 基础镜像使用精确补丁版本与 sha256 digest；`.github/workflows/release.yml` 只接受与 `pyproject.toml` 匹配的 `v<version>` tag，发布单平台 linux/amd64 镜像并把最终 registry digest 绑定到 SPDX JSON SBOM、release manifest 与 SHA256SUMS。
- 详细支持边界见 `docs/upgrade-compatibility.md`，发布资产契约见 `docs/release-process.md`。

## 7. 统一健康与诊断导出

- `GET /api/v1/system/health` 只汇总已存在的本地证据，不因读取健康状态而触发 PT、qBittorrent、Transmission 或通知渠道网络请求，也不遍历媒体树。`/health/ready` 继续只决定当前实例能否安全运行；单个外部依赖离线只在聚合健康中形成 warning，不让容器失去 readiness。
- 聚合项覆盖 runtime、配置卷/数据卷剩余空间、普通备份新鲜度与计划备份状态、RETRY/陈旧活动任务、`RECONCILE_REQUIRED`/`ROLLBACK_BLOCKED`/陈旧未收敛 operation、启用站点的持久探测与熔断状态、启用下载器的连接/路径诊断、通知渠道/DEAD 投递及任务/历史/通知/备份四个后台 driver。
- `GET /api/v1/system/diagnostics/export` 生成内存 ZIP，只包含白名单 `health.json` 与 `manifest.json`。不读取或打包运行日志、配置文件、secret、站点名/URL、下载器地址、路径、任务 ID、source/torrent hash 或媒体内容，并返回 bundle SHA-256 与 `Cache-Control: no-store`。
- 自动化 canary 会把合成 URL、路径、hash、任务 ID 和伪凭证写入底层记录，再证明健康 JSON 与诊断 ZIP 只保留聚合计数，原值均不可见。
- 前端“总览”已经移除旧合成态势，直接消费 typed `/system/health`，按 `ok/warning/blocked` 展示九类健康卡片，并提供安全诊断包下载；30 秒刷新只读取已有证据。

## 8. 有界持久日志

- 生产 server 在继续输出 stdout 单行 JSON 的同时，把同一脱敏 formatter 写入 `/config/logs/packbreaker.jsonl`。目录强制 0700，文件使用 no-follow 安全打开并强制 0600；基础文件若被替换成 symlink，启动即失败关闭。
- 默认单文件上限 2 MiB、保留 4 个轮转文件，近似总容量 10 MiB；可用 `PACKBREAKER_LOG_FILE_MAX_BYTES` 与 `PACKBREAKER_LOG_FILE_BACKUP_COUNT` 在安全范围内调整。message/结构化 fields 也各自有单记录上限，避免异常日志绕过容量设计。
- URL 在写入和读取两侧均只保留 origin，丢弃 userinfo/path/query/fragment；异常只保留类型。`GET /api/v1/system/logs` 默认最近 60 分钟/200 条，最大窗口 7 天、单次最多 500 条；`/system/logs/export` 同一筛选语义且最多 2000 条，并返回 SHA-256 与 `no-store`。
- 前端“日志”页已经移除合成记录，支持安全关键词、级别、15 分钟～7 天窗口过滤和当前窗口导出；查询字符串不会进入 PackBreaker HTTP 日志，因为 HTTP access 摘要只记录 request path。

## 9. 计划备份与管理入口

- `backup_policy` 是 SQLite 中的单例版本化策略，默认 `enabled=false`；周期 1～168 小时、保留 1～3650 天、至少保留 1～100 份。配置更新要求 `config:write` + CSRF（或对应 API Token）并携带强 `If-Match`；后台执行时间与错误状态不会自行递增配置 version。
- BackupDriver 每分钟默认检查一次是否到期；实际备份仍调用同一 SQLite 一致性快照与安全 retention 实现。手动“立即备份”和计划备份共用同一进程内互斥锁，重叠请求返回 busy 而不会并发制造第二份快照。
- “系统设置 → 备份恢复”已接真实策略 API、Driver 状态和手动一致性备份。页面不提供在线数据库恢复；恢复仍要求停止活动实例后使用维护 CLI，且 `secret.key` 必须独立保管。

## 10. 升级中心安全入口

- `GET /api/v1/system/release/preflight` 复用 release preflight 的本地检查，但固定 `exercise_backup=false`，因此页面刷新不会创建/删除备份演练文件；返回配置目录、SQLite head/integrity、现有主密钥、`/data` 根与 docker.sock 风险，不访问 PT/下载器/通知，也不遍历媒体树。
- `GET /api/v1/system/upgrade` 展示当前版本、最新正式 Release/digest、独立 updater helper phase 与自动升级阻断原因；`POST /api/v1/system/upgrade/actions` 要求 `config:write`、管理会话 CSRF（如适用）和 `Idempotency-Key`，并在交给 helper 前重新确认 Release、跑完整 preflight、创建一致性备份。
- `packbreaker-updater` 独占 docker.sock，通过 `/config/updater/updater.sock` + 随机 token 接受主服务请求。主 PackBreaker 自身检测到 docker.sock 时拒绝自动升级。helper 会创建切换瞬间静止数据库备份、按 allowlist 重建容器配置、等待 Docker healthcheck，并在失败时恢复旧数据库/旧容器；无法收敛则进入 `manual_recovery_required`。
- 自动升级当前只支持可安全重建的单容器/单网络拓扑；Docker Compose 管理标签、AutoRemove、container namespace、多网络、显式静态 IP/MAC 等失败关闭。没有 helper 时仍可按不可变 digest runbook 手工升级/回滚。
- CI 新增 `updater-e2e` 真实 Docker 门禁：从 `release-baseline.json` 的正式不可变 baseline 出发，通过 Runner 内临时 registry 给 helper 提供真实 digest pull，分别验证成功替换和“候选修改数据库后 healthcheck 失败”的自动容器/数据库回滚。该门禁不使用生产 `/config`、PT、下载器或媒体目录。

## 11. v1.0 验收证据索引与发布边界

- `docs/v1-acceptance-evidence.json` 按 `V1-001`～`V1-025` 逐项绑定 `docs/testing.md` 的正式验收要求，并记录 `covered`、`field_evidence` 或 `pending_external`。
- `scripts/validate_acceptance_evidence.py` 已进入 `scripts/check.py`；缺失条目、失效文件/锚点、与验收原文脱节的 criterion 或没有 blocker 说明的外部项都会阻断静态门禁。
- `V1-025` 已由数据库备份/升级失败回滚自动化矩阵与 GitHub Actions run `34851129257` 的真实 Docker 空配置启动、离线 restore、重启 readiness 共同闭环；当前验收索引不再有 `pending_external` 条目。
- `docs/support-matrix.md` 与 `docs/known-limitations.md` 已独立发布版本边界与明确遗留，补齐路线图要求的支持版本/已知限制文档。

## 12. 下一阶段退出证据

当前代码侧已取得备份/计划调度、恢复、安全升级矩阵、统一健康仪表盘、持久日志查询/导出与诊断脱敏证据；正式 `v0.1.1` Release workflow run `34924614659` 的镜像、SBOM、manifest、checksums、GitHub Release、`stable` 推进以及 `v0.1.0 → v0.1.1 → 恢复 v0.1.0` 跨版本门禁均已完成。当前项目进入 `0.1.2` 候选开发线，并新增独立 updater helper 的现场一键升级/自动回滚能力；下一项发布级外部证据是让 `v0.1.2` 候选在 GitHub CI / release workflow 中完成 `v0.1.1 → v0.1.2 → 恢复 v0.1.1`。
