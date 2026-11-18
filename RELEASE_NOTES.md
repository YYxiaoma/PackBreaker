# PackBreaker v1.1.7

v1.1.6 的补丁版本，修复一个影响所有 1.1.6 部署的打包缺陷。

## 修复

- **官方内置模板没有随镜像发布**：`Dockerfile` 漏掉了 `resources/`，导致启动时静默跳过内置模板安装，模板库为空、数据拆包「选择模板」下拉框没有任何模板。现在镜像包含 `resources/official_templates/`（周星驰 26 / 成龙 65 / 迪士尼 77 / 豆瓣 IMDb TOP250 430 / 漫威 21，共 619 条种子），启动时幂等安装。
- **内置模板安装失败不再静默**：模板目录或索引文件缺失时写 warning，避免再次出现「界面没数据、日志没线索」。

## 升级说明

- 升级到 1.1.7 后无需手工操作：容器启动时会自动安装内置模板（已安装过的不重复安装，版本变化时追加新版本并保留旧版本）。
- 若之前已在 1.1.6 上手工建过同名模板，内置模板会以独立记录存在，按 `builtin_key` 判定，不会覆盖你的数据。
- 数据库迁移无新增（仍为 `0056_movie_dedup_shared_link_bytes`），无需手工改动。

## 发布工程

- 发布流水线调整：升级与更新门禁全部前移到镜像 push 之前，失败时不会在 registry 留下同名镜像；push 之后的门禁失败会由 `cleanup-published-image` 自动删除本次刚推的版本 tag，不再阻塞下一次重跑。

## 镜像

- `ghcr.io/yyxiaoma/packbreaker:1.1.7`
- `docker.io/yyxiaoma01/packbreaker:1.1.7`
- 支持 `linux/amd64` 与 `linux/arm64`，不可变摘要与 SBOM、SHA256SUMS 见 GitHub Release 附件。
