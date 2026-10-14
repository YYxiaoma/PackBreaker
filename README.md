# PackBreaker

**面向 PT 场景的自动拆包与安全辅种系统。**

从已有媒体目录或 qBittorrent / Transmission 任务出发，完成影片发现、跨站匹配、内容验证、文件复用与做种管理，并提供数据去重、任务监控和故障恢复。

[功能与工作流](#功能全景与工作流) · [快速部署](#快速部署) · [本地开发](#本地开发) · [开发文档](./docs/README.md)

## 项目介绍

PackBreaker 希望让 PT 大包的拆分辅种更省事，也更可靠：**先验证内容，再执行操作；保护原始媒体，保留完整的执行与恢复记录。**

- **自动化**：手动或定时监控任务，集中管理拆包、匹配、审核与辅种。
- **安全性**：Piece 级验证、执行前预检、人工审核、操作日志与故障对账。
- **易管理**：Web 管理端、下载器与站点配置、通知、备份及升级。

## 功能全景与工作流

| 模块 | 主要能力 |
| --- | --- |
| **数据拆包** | 从媒体目录或下载器任务发现影视文件；手动/监控拆包、候选搜索、匹配、审核与重试 |
| **内容验证与辅种** | BitTorrent v1/v2/hybrid 内容校验，Hardlink/COPY 复用，qBittorrent / Transmission 校验与做种 |
| **数据去重** | 扫描重复影片、完整 SHA-256 核验、人工确认与安全硬链接复用 |
| **站点与下载器** | 站点 Cookie/API Key、代理与连接测试；下载器任务、指标及路径映射 |
| **CookieCloud** | 对接 [easychen/CookieCloud](https://github.com/easychen/CookieCloud)，支持凭证同步 |
| **通知与 AI** | Telegram / Server酱通知、站内消息；OpenAI-compatible 模型与 Telegram 只读 AI 助手 |
| **系统运维** | 总览仪表盘、运行日志与诊断、备份恢复、在线升级和异常对账 |

```mermaid
flowchart LR
    A["媒体目录 / 下载器任务"] --> B["发现与跨站匹配"]
    B --> C["内容验证与必要审核"]
    C --> D["安全预检与执行计划"]
    D --> E["Hardlink / COPY"]
    E --> F["客户端下载校验与做种"]
    F --> G["日志、对账与恢复"]
```

> 候选匹配不等于内容通过验证；未满足安全条件的任务会等待审核、重试或阻断。站点能否用于正式辅种，以实际支持状态和验收结果为准。

## 快速部署

推荐使用 **Docker Compose**。将下方内容保存为 `compose.yaml`，按实际情况修改媒体挂载目录和 `PUID` / `PGID`：

```yaml
services:
  packbreaker:
    image: ghcr.io/yyxiaoma/packbreaker:latest
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
      # - /path/to/downloads2:/downloads2
      # 可选：Web 一键升级（授予 Docker 主机管理权限）
      # - /var/run/docker.sock:/var/run/docker.sock

volumes:
  packbreaker-config:
```

```bash
docker compose up -d
```

浏览器访问 **`http://服务器IP:8000`**。默认管理员用户名为 `admin`；首次启动生成的临时密码通过 `docker logs packbreaker` 查看，登录后按提示修改密码。

- 镜像也发布至 Docker Hub：`yyxiaoma01/packbreaker`；生产环境建议固定正式发行版本或不可变 digest。
- 媒体目录须在宿主机存在，并确保 `PUID/PGID` 具备所需访问权限；容器内路径用于下载器路径映射。
- 不挂载 `docker.sock` 时可通过宿主机 `docker compose pull && docker compose up -d` 升级；Web 一键升级需要该高权限挂载，且不会自动更新 `compose.yaml` 的镜像声明。

详见 [部署与运维文档](./docs/README.md#部署与首次配置)。

## 本地开发

需要 **Python 3.11、uv、Node.js 22.12+ 和 Corepack / pnpm**。

安装依赖：

```bash
uv sync --frozen --all-groups
corepack enable
corepack pnpm --dir frontend install --frozen-lockfile
```

启动后端（使用独立开发目录，不复用生产数据）：

```bash
mkdir -p runtime/dev/config runtime/dev/data
PACKBREAKER_CONFIG_DIR="$PWD/runtime/dev/config" \
PACKBREAKER_DATA_DIR="$PWD/runtime/dev/data" \
uv run python -m backend.app.server
```

另开终端启动前端：

```bash
corepack pnpm --dir frontend dev
```

开发页面：**`http://127.0.0.1:5173`**（API 代理到本地 8000 端口）。

运行检查和测试：

```bash
uv run python scripts/check.py
uv run python scripts/test.py
```

更多说明见 [本地开发指南](./docs/README.md#本地开发) 与 [AGENTS.md](./AGENTS.md)。

## 技术栈

| 方向 | 技术 |
| --- | --- |
| 后端 | Python 3.11、FastAPI、SQLAlchemy、Alembic、SQLite |
| 前端 | Vue 3、TypeScript、Vite、Element Plus、Pinia |
| 测试与质量 | pytest、Ruff、mypy、Vitest、Playwright、OpenAPI 类型生成 |
| 构建与交付 | Docker、Docker Compose、GitHub Actions |

## 贡献

欢迎通过 [Issues](https://github.com/YYxiaoma/PackBreaker/issues) 提交问题与建议，或通过 Pull Request 参与开发。提交前请阅读 [AGENTS.md](./AGENTS.md)，并运行与变更范围相关的检查和测试；涉及文件操作或下载器写入的修改须保留安全与恢复机制。

## License

本项目采用 [MIT License](./LICENSE)。
