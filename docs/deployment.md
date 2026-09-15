# 部署、运维与安全

## 1. 部署目标

v1.0 以单个 `linux/amd64` Docker 镜像发布，单容器内运行 FastAPI、前端静态资源、调度器和单进程 worker。SQLite、日志、密钥与备份保存在 `/config`，媒体和硬链接目标通过 `/data` 暴露。

首版不支持 Kubernetes、多副本或共享数据库。容器必须使用 init/锁机制确保同一 `/config` 只有一个活动 worker。

## 2. 镜像结构

仓库根目录 `Dockerfile` 已采用三阶段构建：

1. Node 22 + pnpm 10.34.5 按冻结锁文件构建 Vue 前端。
2. Python 3.11 + uv 0.12.11 按 `uv.lock` 的 runtime 依赖构建 `/opt/venv`，不安装 dev group。
3. 精简 Python 3.11 runtime 只复制运行 venv、后端/迁移和前端 dist，并由 FastAPI 同源服务 `/` 与 `/assets/*`。

运行镜像要求：

- 镜像默认 `USER packbreaker`（UID/GID 1000）。Compose 为初始化 named `/config` 卷可短暂以 root 运行入口包装器；包装器只递归调整应用专属 `/config` 所有权，立即清空 supplementary groups 并按 `PUID`/`PGID` 永久降权后才导入/启动服务。`/data` 不自动 chown。
- 只包含运行依赖，不包含测试工具、源码缓存、Node modules 和真实配置。
- 基础 Node/Python 镜像使用精确补丁版本 + sha256 digest 固定；正式发布按最终镜像 digest 生成 SPDX JSON SBOM。可移动 `stable` 只作发现通道，不能作为生产部署身份。
- OCI 标签包含版本、Git revision、构建时间、源码地址和许可证。
- 入口先验证配置和迁移，再启动应用；迁移失败不得启动 worker。
- tag 发布由 `.github/workflows/release.yml` 生成最终 GHCR image digest、SPDX JSON SBOM、release manifest、`SHA256SUMS` 与 GitHub Release notes；生产部署记录必须使用 release manifest 的完整 `<image>@sha256:<digest>`。

## 3. 目录与权限

| 容器路径 | 内容 | 权限 |
| --- | --- | --- |
| `/config` | SQLite、加密密钥文件、日志、备份、运行锁 | 应用用户读写，目录建议 0700 |
| `/data` | 媒体源、硬链接目标、受控 staging | 按路径策略读/写；不能包含应用秘密 |
| `/tmp` | 临时解析和导出 | 容器临时空间，定期清理 |
| `/var/run/docker.sock` | 可选升级能力 | 默认不挂载；挂载即授予 Docker 管理权限 |

硬链接要求源与目标在内核允许的同一挂载/文件系统内。部署时应把源目录和目标目录的共同父目录一次性挂载到 `/data`，再通过应用允许根目录限制访问。把两个子目录分别 bind mount 可能产生 `EXDEV`，即使宿主机底层存储相同。

应用启动和保存下载器配置时执行路径诊断：容器可见性、device ID、普通文件、权限、符号链接、创建临时硬链接和清理测试。诊断未通过的映射不能启用自动执行。

## 4. 启动配置

仅保留无法安全放入数据库或启动前必须知道的环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PACKBREAKER_HOST` | `0.0.0.0` | 监听地址 |
| `PACKBREAKER_PORT` | `8000` | HTTP 端口 |
| `PACKBREAKER_CONFIG_DIR` | `/config` | 配置与状态目录 |
| `PACKBREAKER_DATA_DIR` | `/data` | 数据根目录 |
| `PACKBREAKER_FRONTEND_DIR` | 镜像内 `/app/frontend/dist`；源码运行默认空 | 前端静态构建目录；设置时必须为绝对路径且包含 `index.html`/`assets` |
| `PACKBREAKER_SECRET_KEY_FILE` | `/config/secret.key` | 主密钥文件；首次启动安全生成 |
| `PACKBREAKER_LOG_LEVEL` | `INFO` | 日志级别 |
| `PACKBREAKER_TIMEZONE` | `Asia/Shanghai` | 仅影响调度和展示，数据库仍保存 UTC |
| `PACKBREAKER_TRUSTED_PROXIES` | 空 | 明确列出的反向代理网段 |
| `PACKBREAKER_TASK_DRIVER_INTERVAL_SECONDS` | `15` | 活动任务周期 tick 间隔，范围 1..3600 秒 |
| `PACKBREAKER_TASK_DRIVER_LIMIT` | `100` | 每次 tick 最多扫描的活动任务数，范围 1..1000 |
| `PACKBREAKER_TASK_DRIVER_MAX_STEPS_PER_TASK` | `4` | 单任务每次 tick 最多连续推进的 stage 数，范围 1..16 |

环境变量不得直接承载站点 passkey、下载器密码和通知 Token。首次主密钥使用密码学安全随机源生成 256-bit 随机值，文件权限设为 0600；文件已存在但权限过宽或长度无效时拒绝启动 worker。secret store 使用 AES-256-GCM，认证附加数据绑定 secret ID、类型和密钥版本，数据库只保存认证密文。

## 5. Compose 蓝图

仓库根目录 `compose.yaml` 可直接用于本地构建/启动：

```bash
export PACKBREAKER_DATA_PATH=/path/to/common/storage
export PUID=1000  # 可设为 0，以 root UID 运行服务进程
export PGID=1000  # 可设为 0，以 root GID 运行服务进程
docker compose up --build -d
```

Compose 使用 `packbreaker-config` named volume 保存 SQLite、主密钥和锁，数据根通过 `PACKBREAKER_DATA_PATH` 显式 bind mount 到 `/data`；默认 HTTP 端口为 8000，可用 `PACKBREAKER_HTTP_PORT` 修改宿主机端口。容器设置 `no-new-privileges`，健康检查执行 `python -m backend.app.healthcheck`，只请求本机 `/api/v1/health/ready`。

如果使用自定义 `PUID`/`PGID`，应确保 `/data` 内需要读取的源文件和允许创建目标链接的目录对该数字身份有适当权限。`0` 是有效值；`PUID=0`、`PGID=0` 时服务进程保持 root 身份运行。入口不会为了方便而修改媒体树所有权。升级中心需要 docker.sock 时仍必须由用户在部署文件中显式增加挂载；默认 Compose 不授予 Docker 管理权限。

## 6. 网络与反向代理

- 推荐只监听内网或通过 HTTPS 反向代理访问；公网暴露不属于 v1.0 支持范围。
- 只有 TCP 直连来源命中 `PACKBREAKER_TRUSTED_PROXIES` 中的 IP/CIDR 时才接受 `X-Forwarded-For` 与 `X-Forwarded-Proto`；客户端地址从代理链右侧逐级剥离可信代理，非法链回退到直连地址。
- 会话 Cookie 在直接 HTTPS 或受信代理声明 HTTPS 时使用 Secure；未受信来源不能靠伪造转发头启用代理上下文。反代仍需保留 Host、真实 scheme 和 SSE 长连接。
- 出站访问仅允许已配置站点、下载器、通知和版本检查目标；日志不得记录带查询秘密的 URL。
- 响应统一设置 CSP、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`X-Frame-Options: DENY` 与 Permissions-Policy；HTTPS 上额外设置 HSTS。Swagger 文档只放行自身、必要 CDN 和 OpenAPI 请求。

## 7. 数据库、备份与恢复

### 7.1 备份

- 使用 SQLite Backup API 生成一致性快照，不能只复制活跃 WAL 下的主 `.db` 文件。
- 当前已提供 `python -m backend.app.maintenance backup`；默认输出到 `/config/backups`（源码运行时为 `<PACKBREAKER_CONFIG_DIR>/backups`）。每个 `.db` 快照同时生成 `.json` manifest，记录 SHA-256、字节数、应用版本和 Alembic revision；可用 `python -m backend.app.maintenance verify-backup <backup.db>` 离线复验。
- 升级前强制备份；另支持手动和计划备份。
- 备份包含数据库、schema/app 版本、校验摘要和必要的非敏感部署元数据。
- 主密钥不默认打包；包含凭证的可迁移导出必须由用户提供独立口令再次加密。
- 备份先写临时文件、fsync 后原子重命名，并按保留策略清理。

当前 M6 已覆盖一致性数据库快照、离线验证、下述离线恢复底座、普通备份保留策略、默认关闭的计划备份调度与管理界面；GitHub Actions container 门禁也已完成停止服务、离线 verify/restore、重启 readiness 的真实 Docker 演练。

普通备份保留默认只预览：

```bash
python -m backend.app.maintenance backup-retention --retention-days 30 --keep-latest 3
python -m backend.app.maintenance backup-retention --retention-days 30 --keep-latest 3 \
  --apply --confirm-delete-expired-backups
```

保留策略只处理 `/config/backups` 根目录中可完整验证的 PackBreaker `.db/.json` 成对备份；不会递归进入 `pre-restore/`，也不会删除外来、孤儿或损坏文件。

### 7.2 恢复

恢复流程必须在 worker 停止时执行：验证摘要和版本兼容，备份当前状态，恢复到临时位置，运行数据库完整性检查和只读迁移预检，再原子切换。失败时恢复原状态。

当前 M6 已提供离线恢复命令：

```bash
python -m backend.app.maintenance restore-backup /config/backups/<backup>.db \
  --confirm-replace-current-database
```

恢复命令与主服务复用同一实例锁；只要活动 PackBreaker 仍持有 `/config/packbreaker.lock` 就会失败关闭。恢复前会自动创建 `backups/pre-restore` 一致性安全快照，待恢复副本在临时文件上执行 Alembic `upgrade head`、完整性检查和 journal 规范化后才原子切换；切换后验证失败会自动从安全快照回滚。GitHub Actions run `34851129257` 已在目标 linux/amd64 Docker 方式完成停止服务、一次性恢复、重启与 readiness 实机演练。

主密钥丢失时，密文凭证不可恢复；任务和非敏感配置仍应可读，但所有依赖凭证的适配器必须禁用并要求重新录入。

### 7.3 发布前 preflight

发布/升级前先运行：

```bash
python -m backend.app.maintenance preflight
```

该命令不访问 PT、下载器、通知渠道，也不遍历媒体树。它检查 `/config` 权限、SQLite `integrity_check` 与 Alembic head、现有 `secret.key` 0600/32-byte 与 AES-GCM 自检、`/data` 根可见性、默认 docker.sock 风险，并实际创建/验证/清理一次 preflight 一致性快照。任一关键项 blocked 时退出码为 2。`--skip-backup-exercise` 只用于明确不允许写入 `/config/backups` 的诊断场景，并会留下 warning。

### 7.4 Docker Compose 备份与恢复 runbook

以下命令假设仓库根目录的 `compose.yaml`，并保持维护命令使用与服务一致的数字 UID/GID，避免在 named volume 内产生 root-owned 状态：

```bash
export PUID=${PUID:-1000}
export PGID=${PGID:-1000}

# 服务运行时先做发布预检和一致性备份
docker compose exec --user "$PUID:$PGID" packbreaker \
  python -m backend.app.maintenance preflight
docker compose exec --user "$PUID:$PGID" packbreaker \
  python -m backend.app.maintenance backup

# 记录上一步 JSON 返回的 database_file，然后停止服务
docker compose stop packbreaker

# 离线复验并恢复指定备份；restore 会再创建 pre-restore 安全快照
docker compose run --rm --no-deps --user "$PUID:$PGID" packbreaker \
  python -m backend.app.maintenance verify-backup /config/backups/<backup>.db
docker compose run --rm --no-deps --user "$PUID:$PGID" packbreaker \
  python -m backend.app.maintenance restore-backup /config/backups/<backup>.db \
  --confirm-replace-current-database

# 重启并检查 readiness
docker compose up -d packbreaker
docker compose exec packbreaker python -m backend.app.healthcheck
```

如果最后一步 readiness 失败，不要删除 `backups/pre-restore`。先停止服务，再使用恢复命令把本次 `pre-restore` 安全快照恢复回去，随后启动原镜像并重新检查 readiness。旧镜像无法读取新迁移数据库时也必须走备份恢复，不能只切镜像标签。

### 7.5 计划备份

计划备份策略持久化在 PackBreaker SQLite 中，默认关闭。启用后 BackupDriver 周期读取策略，只在最近成功备份超过配置周期时创建普通一致性备份；它不连接 PT、下载器、通知渠道或扫描 `/data`。手动“立即备份”与计划任务共用同一锁，避免重复并发快照。管理 API/UI 可配置 1～168 小时间隔、1～3650 天保留期和至少保留 1～100 份；实际清理继续复用标准 pair 校验与 no-follow 安全门。

计划任务仅备份数据库，不复制 `/config/secret.key`。数据库中的凭证仍是密文，但灾难恢复若没有原主密钥将无法解密，因此 `secret.key` 必须用独立受控方式备份。在线 UI 不提供数据库恢复；恢复必须按 7.4 停服务执行。

## 8. 升级与回滚

```mermaid
flowchart LR
    A[检查版本/兼容矩阵] --> B[拉取不可变镜像]
    B --> C[一致性备份]
    C --> D[停止 worker]
    D --> E[启动新镜像并迁移]
    E --> F{就绪与自检通过?}
    F -- 是 --> G[恢复调度并完成]
    F -- 否 --> H[停止新镜像]
    H --> I[恢复兼容数据库]
    I --> J[启动旧镜像]
```

当前 RuntimeManager 的数据库启动升级已经使用安全切换底座：取得实例锁后，若数据库落后于代码 head，先创建 `/config/backups/pre-upgrade` 一致性安全快照，再把快照复制到同文件系统临时文件执行 Alembic `upgrade head`、journal 规范化、`integrity_check` 与 head 校验；全部通过后才原子替换当前数据库。迁移副本失败时当前数据库不发生切换，切换后校验失败会自动恢复安全快照。空配置首次安装同样先迁移临时数据库，通过后才原子安装，因此迁移异常不会留下半完成目标数据库。

当前自动化兼容矩阵已覆盖仓库全部 22 个历史 revision（`0001`～`0022`）升级到 `0023_backup_policy`。生产回滚不依赖 Alembic 原地 downgrade：若旧镜像不能读取新 schema，必须恢复 `pre-upgrade`/升级前备份后再启动旧镜像。完整矩阵见 `docs/upgrade-compatibility.md`。

从 `v0.1.0` 开始，仓库还固定 `release-baseline.json` 作为上一正式镜像的不可变升级输入。普通 CI container gate 与正式 tag release 在发布新镜像前都会运行 `scripts/check-release-upgrade.sh`：真实启动基线 digest、创建合成状态和升级前备份、让当前候选接管并 readiness，然后用基线镜像恢复旧备份并重新启动旧镜像。该门禁只操作 CI 临时目录，不连接 PT、下载器或真实媒体；当项目版本进入 `0.1.1+` 后，它即构成正式 release-to-release 升级/回滚证据。

- 版本与目标镜像只信任正式 release manifest、`SHA256SUMS` 和 registry 返回的不可变 digest；不能仅依赖可变 `latest` 或版本 tag。
- 升级中心的 `GET /system/release/preflight` 只做本地只读检查，并固定跳过临时备份演练；正式切换镜像前仍需运行 `python -m backend.app.maintenance preflight` 完成一致性备份演练。
- 新版本健康检查包含迁移版本、数据库/secret 自检和实例/worker 运行条件，不要求所有 PT 站在线。
- 如果新迁移不可被旧版本读取，必须通过升级前或 `pre-upgrade` 备份恢复，不能只切换旧镜像。
- Web 升级中心无论 docker.sock 是否存在都不会调用 Docker API；默认无 docker.sock 时展示相同的宿主机手工 digest 切换步骤，避免把容器管理权限引入应用进程。

## 9. 日志、指标与告警

- runtime 同时向 stdout 与 `/config/logs/packbreaker.jsonl` 输出同一格式的单行 JSON。stdout 继续由 Docker/宿主日志驱动负责保留；应用文件采用容量轮转，默认单文件 2 MiB + 4 个备份（近似总容量 10 MiB），分别可由 `PACKBREAKER_LOG_FILE_MAX_BYTES`、`PACKBREAKER_LOG_FILE_BACKUP_COUNT` 调整。日志目录强制 0700、文件 0600，写入/读取均拒绝跟随符号链接。
- 应用 HTTP 日志只记录 trace_id、方法、路径（不含 query）、状态、来源摘要与耗时；异常只记录异常类型。INFO 记录业务阶段与结果，DEBUG 也不得输出秘密、torrent payload 和第三方原始响应。通用脱敏会屏蔽敏感 key/value；URL 只保留 origin，不保留 userinfo、path、query 或 fragment；单条 message/fields 也有大小上限。
- `GET /api/v1/system/health` 要求管理员会话或 `config:read` API Token，只汇总已有证据：runtime readiness、配置/数据卷剩余空间、普通备份新鲜度、任务积压、operation 对账风险、站点熔断/最近连接状态、下载器连接/路径诊断、通知与后台 driver。读取该端点不会主动连接 PT、qB/TR、通知服务，也不会遍历 `/data` 媒体树。
- `/health/ready` 与 `/system/health` 语义分离：前者决定实例能否安全承载工作，后者表达运维告警。站点或下载器离线不会把容器 readiness 变成失败。
- `GET /api/v1/system/diagnostics/export` 使用同一 `config:read` 权限，内存生成只包含 `health.json`/`manifest.json` 的 ZIP，响应 `Cache-Control: no-store` 与内容 SHA-256。诊断包仍与日志导出严格分离，不读取运行日志、配置文件、secret 或媒体，也不输出 URL、业务路径、任务 ID、source/torrent hash。
- `GET /api/v1/system/logs` 读取应用自身轮转 JSONL，默认最近 60 分钟/200 条；允许窗口最大 7 天、单次最多 500 条，并支持级别和最长 128 字符安全关键词过滤。`GET /api/v1/system/logs/export` 复用同一筛选规则，导出硬上限 2000 条，返回内容 SHA-256 和 `Cache-Control: no-store`。读取端再次执行脱敏，因此历史格式异常或人为写入的敏感字段不能直接透传到 API。
- 通知对重复错误聚合，避免站点故障造成消息风暴。
- 当前健康告警阈值：普通备份超过 7 天视为 stale；非终态任务超过 24 小时未更新、未收敛 operation 超过 1 小时进入 warning；配置卷或数据卷剩余空间低于 2 GiB 或 10% 进入 warning。这些阈值仅影响运维态势，不绕过业务安全门。

## 10. 安全检查清单

- 管理口令已设置，默认口令不存在。
- `/config` 与 secret.key 权限正确，备份存储位置受控。
- 站点、下载器、通知测试日志和诊断包通过秘密 canary 测试。
- `/data` 只映射必要共同父目录，应用允许根目录配置正确。
- docker.sock 默认未挂载；启用时已明确风险并限制管理界面访问。
- 真实 `.torrent`、数据库和日志未进入镜像或源码仓库。
- 恢复、升级失败回滚和主密钥丢失场景已经演练。
