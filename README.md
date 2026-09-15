# PackBreaker

PackBreaker 是一个面向 PT 场景的自动拆包辅种系统。它以“大包”下载任务为输入，将内容识别为单影片或单集单元，跨站搜索匹配资源，并通过硬链接复用已有数据完成辅种。

## 核心能力

- 接入 qBittorrent 与 Transmission，支持多个下载器实例
- 监控大包任务并执行解析、搜索、匹配、硬链接、添加辅种和状态确认
- 通过统一适配器接入多个 PT 站点，首批计划支持 M-Team、HDTime 和 HHClub
- 提供失败重试、99% 卡死修复、缺失附属文件修复和人工确认
- 支持历史影片扫描辅种与电视剧按季、集拆包辅种
- 提供任务管理、配置、日志、通知和升级界面

## 技术方向

- 后端：Python 3.11、FastAPI、SQLAlchemy、Alembic、SQLite、APScheduler
- 前端：Vue 3、Vite、Element Plus、Pinia、Axios、ECharts
- 部署：Docker 单镜像，首个正式版本面向 `linux/amd64`

## 项目状态

PackBreaker 已完成 M6 发布与运维闭环的主要能力，并已正式发布 `v0.1.1`。该版本的 linux/amd64 不可变镜像为 `ghcr.io/yyxiaoma/packbreaker@sha256:76f4c041d1acecbb573cdbd49c45aec936bfd8cf3b263bc09153f7741f18e30d`；Release workflow run `34924614659` 已真实通过 `v0.1.0 → v0.1.1 → 恢复 v0.1.0` 的跨版本 Docker 门禁。

当前源码进入 `0.1.2` 候选开发线。除既有 qBittorrent/Transmission 主链、M-Team/HDTime/HHClub、v1/v2/hybrid piece 验证、人工审核、journal-backed 执行/取消/回滚、历史扫描、repair、备份恢复、健康/日志/诊断等能力外，升级中心正在加入独立 `packbreaker-updater` helper：主 PackBreaker 不持有 docker.sock，helper 负责按正式 Release 的不可变 digest 拉取镜像、重建容器、等待 healthcheck，并在失败时恢复切换瞬间数据库备份与旧容器。

发布、升级与支持边界见 `docs/deployment.md`、`docs/upgrade-compatibility.md`、`docs/support-matrix.md` 与 `docs/known-limitations.md`。

### 本地开发

准备 Python 3.11、Node.js 22.12+ 与 pnpm 10.34.5：

```bash
corepack pnpm --dir frontend install --frozen-lockfile
corepack pnpm --dir frontend dev
```

浏览器访问 `http://127.0.0.1:5173/`。安装完整开发依赖后，可运行 `uv run python scripts/check.py` 执行静态检查与 OpenAPI 生成漂移检查，运行 `uv run python scripts/test.py` 执行后端/前端测试和生产构建。

Docker 环境可直接运行：

```bash
docker compose up --build -d
```

生产环境应把 `PACKBREAKER_IMAGE` 固定为正式 Release manifest 给出的完整 `@sha256:` digest，而不是依赖可移动 tag。Compose 管理的主容器继续采用宿主机显式更新 digest；Web 一键升级只支持独立 `docker run --name packbreaker` 部署，并需要另行启动只持有 Docker socket 的 `packbreaker-updater` helper，完整命令见 `docs/deployment.md`。

完整需求请参阅[需求基线 v0.3](./自动拆包辅种系统-需求基线-v0.3.html)。历史版本保留在[需求基线 v0.2](./自动拆包辅种系统-需求基线-v0.2.html)。研发设计、接口规范、测试计划和实施路线请参阅[研发文档索引](./docs/README.md)。

## 许可证

本项目采用 [MIT License](./LICENSE)。
