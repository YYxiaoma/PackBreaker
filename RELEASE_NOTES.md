# PackBreaker v1.1.9

修复 v1.1.7 起「在线升级一次之后就再也无法在线升级」的问题。

## 修复

- **升级状态文件的权限与误判**：一次性 updater helper 以 root 运行，升级结束后把 `/config/transient-updater/state.json` 写成 `root:root 0600`；而以 PUID 运行的主应用（Synology 等部署常见，如 1026:100）读不到它，于是把「读不到」误判成「状态文件损坏」，向界面报告
  `UPDATER_MANUAL_RECOVERY_REQUIRED` —— 表现为「上一次升级现场需要人工核对，当前禁止继续自动升级」，升级按钮永久置灰。

  现在的处理：

  1. helper 写状态后统一把文件设为 `0644`，运行在 root 时还会把属主对齐到配置目录（即主应用的 PUID/PGID）；
  2. 主应用遇到权限导致的读取失败时，不再当作「需要人工恢复」，而是把不可读的旧状态文件改名为 `state.unreadable-<时间>.json` 留档，然后回到 idle，允许重新发起升级。

  受影响的部署（已经升级过一次、按钮变灰）升级到 1.1.9 后会自动恢复，无需手工改权限。

- **镜像拉取失败自动重试**：在线升级从 `ghcr.io` 拉取目标镜像时，registry 或代理抖动会直接让升级失败（现场表现为点击升级后几秒内报「目标镜像拉取失败」，旧容器仍在运行）。现在拉取失败会退避重试 3 次，仍失败才判定升级失败；同时把 daemon 返回的失败详情带进提示，便于判断是网络还是权限问题。

## 升级说明

- 若当前按钮已被置灰，无法走应用内升级：请先 `docker compose pull && docker compose up -d` 拉到 1.1.9，之后在线升级恢复正常。
- 也可以手工清理一次旧状态文件：`chmod 644 /config/transient-updater/state.json`（或 `chown 1026:100`）。
- 无数据库迁移（仍为 `0056_movie_dedup_shared_link_bytes`）。

## 包含 v1.1.8 的内容

- 不同站点可同时执行「立即同步」
- 模板导入驱动在数据库锁竞争时指数退避、日志限流
- 在线升级进度条在容器切换期间保持显示，成功后有明确提示与「刷新页面」按钮

## 镜像

- `ghcr.io/yyxiaoma/packbreaker:1.1.9`
- `docker.io/yyxiaoma01/packbreaker:1.1.9`
- 支持 `linux/amd64` 与 `linux/arm64`，不可变摘要、SBOM 与 SHA256SUMS 见 GitHub Release 附件。
