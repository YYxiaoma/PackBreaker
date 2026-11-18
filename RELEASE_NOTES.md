# PackBreaker v1.1.11

种子暂存从媒体数据根搬进 config 卷：`PUID` 不再是隐藏前提，换过 Cookie 之后也不会再卡在「无法安全保存已获取的候选 torrent」。

## 修复

- **修复内容校验前的种子暂存失败**：此前 `.torrent` 暂存写在 `PACKBREAKER_DATA_DIR`（默认 `/data`）下，而镜像把该目录属主固化为 1000；容器实际以 `PUID` 身份运行，只要 `PUID` 不等于 1000（群晖首个账号通常是 1026），取种成功后写盘必然 `EACCES`，每条候选都会停在内容校验之前，报「无法安全保存已获取的候选 torrent，暂不继续内容校验」。
- 暂存目录改为 `/config/torrents`：入口脚本在降权前已对 config 卷递归对齐 `PUID`/`PGID`，因此任何 `PUID` 都能直接写入，且随 `/config` 一起持久化与备份，不再需要额外挂载数据卷。
- 启动时一次性迁移旧版 `data 根/unpack-manual-torrents` 下的暂存文件：按内容哈希校验后搬入新根，摘要不符或不可读的条目保持原样跳过；内容寻址的 key 与根目录无关，数据库引用无需改动。
- 健康检查新增 `torrent_store` 项：目录不可写时直接报 `TORRENT_STORAGE_UNAVAILABLE`，在启动后就暴露，不必等到第一次取种。

## 变更

- 新增 `PACKBREAKER_TORRENT_STORE_DIR`（绝对路径）可自定义暂存根，默认 `/config/torrents`。
- README 与部署指南明确：种子暂存随 config 卷持久化，**不需要**为它单独挂载数据卷，也不要用命名卷方案。

## 升级说明

- 无数据库迁移（仍为 `0056_movie_dedup_shared_link_bytes`）。
- 升级后旧目录的暂存文件会自动搬迁；容器内层 `/data` 未挂载时，这些文件原本也会在重建时丢失。
- v1.1.10 及更早的镜像同样存在该暂存目录问题，只是要等到第一次取种成功才会暴露，建议一并升级。

## 包含 v1.1.10 的内容

- 目录选择器支持「新建文件夹」与「刷新」（数据拆包、数据去重两处）
- 新建站点默认请求超时 60 秒、搜索间隔 30 秒，切换类型时与配置档取较大者
- v1.1.9：在线升级状态修复、镜像拉取失败退避重试

## 镜像

- `ghcr.io/yyxiaoma/packbreaker:1.1.11`
- `docker.io/yyxiaoma01/packbreaker:1.1.11`
- 支持 `linux/amd64` 与 `linux/arm64`，不可变摘要、SBOM 与 SHA256SUMS 见 GitHub Release 附件。
