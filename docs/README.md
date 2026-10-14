# PackBreaker 项目说明

本文依据 v1.1.2 当前实现整理，作为使用与维护入口。历史开发流水、里程碑清单和旧 UI 原型已从文档目录清理；历史版本可通过 Git tag 和 GitHub Releases 查阅。

## 当前版本与边界

v1.1.2 已正式发布，源码提交为 `970cbdd199cf4f9e459ccf76bf23c982d2fa16e6`，提供 Linux AMD64 与 ARM64 镜像。

- GHCR：`ghcr.io/yyxiaoma/packbreaker`
- Docker Hub：`yyxiaoma01/packbreaker`
- [正式发行与附件](https://github.com/YYxiaoma/PackBreaker/releases/tag/v1.1.2)
- [发布流水线](https://github.com/YYxiaoma/PackBreaker/actions/runs/38545506607)

七个新增站点已开放为 `SUPPORTED`；这表示代码适配与任务准入，不表示完整真实业务链已逐站验收。七站正式完整 E2E 收据仍为 **0/7**，本次发布采用用户明确接受该缺口的策略。网络、Tracker 与异常恢复现场验证留待后续版本，见 [已知限制](known-limitations.md)。

## 功能入口

| 入口 | 当前能力 |
| --- | --- |
| 总览 | 系统资源、站点与下载器连接、任务生命周期概览 |
| 数据拆包 | 手动与监控任务、影片发现、跨站匹配、候选审核、内容验证、受控物化、客户端下载校验及做种 |
| 数据去重 | 重复影片预检、完整内容哈希、审核与受控文件复用 |
| 站点管理 | 固定站点 Profile、凭证、代理、连接测试，与 CookieCloud 双卡切换 |
| 下载器 | qBittorrent / Transmission 配置、连接状态、任务与路径映射 |
| 通知 | Telegram、Server酱、站内 Inbox |
| AI 助手 | OpenAI-compatible Provider、模型、只读 Tools、Telegram 对话 |
| 系统运维 | 日志、健康诊断、备份恢复、版本检查及受控升级 |

自动批准保持关闭；候选分数或文件名相同不能替代内容验证和执行安全门。

## 部署与首次配置

推荐 Docker Compose。将以下内容保存为 `compose.yaml`，替换媒体路径、UID 和 GID：

```yaml
services:
  packbreaker:
    image: ghcr.io/yyxiaoma/packbreaker:1.1.2
    container_name: packbreaker
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      PUID: "1000"
      PGID: "1000"
      PACKBREAKER_TIMEZONE: "Asia/Shanghai"
    volumes:
      - packbreaker-config:/config
      - /path/to/downloads:/downloads
    security_opt:
      - no-new-privileges:true

volumes:
  packbreaker-config:
```

```bash
docker compose pull
docker compose up -d
docker logs packbreaker
```

浏览器访问 `http://服务器IP:8000`。管理员用户名为 `admin`，首次临时密码从容器启动日志获取，登录后按提示改密。生产部署建议固定版本或发行附件中的不可变 digest。

1. 持久保存 `/config`，其中包含数据库、配置及受保护凭证所依赖的数据；迁移和恢复前先备份。
2. 将媒体目录挂载为明确的容器路径。界面使用容器路径，下载器保存路径通过路径映射对应。
3. 配置下载器并测试连接，再配置站点 Cookie / API Key 或 CookieCloud。
4. 从小范围手动任务开始，检查发现结果、候选、内容校验和计划后再运行。
5. 监控任务涉及周期触发，应先验证来源范围、目标路径与下载器配置。

仓库根目录的 Compose 默认构建本地镜像；上述示例使用已发布镜像，二者用途不同。

### 路径、权限与数据保护

- 宿主机媒体路径与容器路径必须对应，下载器也必须能访问最终保存位置。
- Hardlink 要求源与目标在同一文件系统；无法硬链接时依实际执行计划与授权使用 COPY。
- 源媒体不允许被下载器修改。可能恢复下载的目标必须先隔离为独立 inode。
- 下载器已有相同 hash 不代表可被认领，还需要路径、归属和 journal 证据。
- 不直接手工修改运行数据库、journal 或任务状态以跳过阻断。

### 升级与恢复

未挂载 Docker socket 时，在宿主机执行：

```bash
docker compose pull
docker compose up -d
```

Web 一键升级需要显式挂载 `/var/run/docker.sock:/var/run/docker.sock`，这会授予容器 Docker 主机管理权限。升级前检查健康状态、备份与目标镜像身份；Web 升级不会自动改写宿主机 Compose 的镜像声明。失败后按升级记录检查旧容器和数据库恢复结果，不能只通过更换镜像标签推断数据库已回滚。

备份使用系统设置中的备份功能或已提供的 maintenance 命令；恢复时保持应用停止或遵循维护工具的离线约束，避免复制运行中的 SQLite 文件造成不一致。

## 数据拆包执行过程

任务定义和来源扫描生成影片项，随后进行跨站搜索、详情解析和 torrent 元信息匹配。审核与内容验证通过后，后端冻结执行计划，再按计划物化文件、添加下载器任务、验证客户端状态并启动做种。

- 匹配结果只是候选；`FULL_VERIFIED` 必须来自所要求的完整内容验证。
- Transmission 需要独立 verify；qBittorrent 是否可跳过部分校验取决于真实能力与有效证据。
- 外部操作先记录 journal intent，再执行并记录结果。
- 响应丢失时先读取真实状态并对账，不能盲目重复 ADD / START。
- 取消、回滚和资源释放只能处理被当前计划与 journal 证明归属的对象。
- 手动与监控任务共用业务执行链；任务定义、execution、影片项和外部操作有各自状态。

## 架构与代码导航

| 位置 | 职责 |
| --- | --- |
| `backend/app/api/` | FastAPI 路由、认证、请求及响应契约 |
| `backend/app/application/` | 拆包、去重、站点、下载器、通知、升级等流程 |
| `backend/app/domain/` | 领域状态、匹配规则、安全不变量与执行计划 |
| `backend/app/infrastructure/` | 外部协议适配、文件系统及持久化 |
| `backend/migrations/` | Alembic 数据库迁移 |
| `frontend/src/components/` | Vue 管理界面，任务入口为 TaskCenterV2 |
| `frontend/src/api/` | API 调用与生成的 TypeScript 类型 |
| `tests/` | 单元、API、应用、契约及集成测试 |
| `scripts/` | 检查、测试、契约生成、隔离验收和发布辅助 |
| `.github/workflows/` | CI、容器验证、候选交付及正式发布 |

后端为 Python 3.11 / FastAPI / SQLAlchemy / Alembic / SQLite，前端为 Vue 3 / TypeScript / Vite / Element Plus / Pinia。数据库迁移、OpenAPI 与生成类型必须随接口变更同步。

## 本地开发

需要 Python 3.11、uv、Node.js 22.12+、Corepack，前端锁定 pnpm 10.34.5。

```bash
uv sync --frozen --all-groups
corepack enable
corepack pnpm --dir frontend install --frozen-lockfile
mkdir -p runtime/dev/config runtime/dev/data
PACKBREAKER_CONFIG_DIR="$PWD/runtime/dev/config" \
PACKBREAKER_DATA_DIR="$PWD/runtime/dev/data" \
uv run python -m backend.app.server
```

另开终端执行 `corepack pnpm --dir frontend dev`，访问 `http://127.0.0.1:5173`，API 代理到本地 8000 端口。开发目录不得复用生产数据库、凭证或媒体。

```bash
uv run python scripts/check.py
uv run python scripts/test.py
```

前者执行仓库扫描、证据和基线检查、Ruff、mypy、前端 lint/typecheck 及 OpenAPI 漂移检查；后者运行后端测试、前端测试和构建。UI 变更还应运行相关浏览器门禁，详见 [测试与验收](testing.md) 和根目录 [AGENTS.md](../AGENTS.md)。

## 故障排查

| 现象 | 优先检查 |
| --- | --- |
| 搜索或详情超时 | 站点网络、凭证、代理、限流、实际 Profile 超时；保留稳定错误码 |
| 文件找不到或无写权限 | 容器挂载、路径映射、PUID/PGID、目标根权限 |
| 目标冲突或计划 stale | 当前源/目标快照、配置版本与计划身份；重新预检 |
| 客户端校验未完成 | 客户端实际进度、暂停状态、保存路径、verify journal |
| 未决操作或重启恢复阻断 | 日志、执行记录与 journal 对账，先确认真实外部状态 |
| 升级失败 | 发行 manifest/digest、Docker 权限、健康检查、备份及回滚记录 |

提交问题时提供版本、架构、错误码、复现步骤和脱敏日志，不上传 Cookie、Passkey、API Key、真实 torrent、数据库或完整私有媒体路径。

## 文档与证据维护

- [站点支持边界](support-matrix.md)
- [已知限制](known-limitations.md)
- [测试与验收](testing.md)

CI 依赖的 JSON 清单和 v1.1.2 发布授权记录继续保留，不能通过清理文档篡改验收结果。旧验收索引所需的原始材料迁至 `evidence/legacy/`，仅用于已有证据校验，不作为当前操作指南。已发布 tag 与 Release 附件保持不变。后续维护以 main 和当前实现为准，文档更新不能代替真实环境验证。
