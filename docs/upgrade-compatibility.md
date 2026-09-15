# 数据库升级兼容矩阵

## 1. 当前基线

当前项目候选版本为 `0.1.2`，Alembic head 为 `0023_backup_policy`。升级兼容性以数据库 revision 和可恢复证据为准，不以镜像标签名称猜测。

## 2. 自动化兼容矩阵

| 来源状态 | 目标 | 自动化结论 | 回滚边界 |
| --- | --- | --- | --- |
| 空配置、无数据库 | 当前 head | 支持；先在同文件系统临时数据库执行全量迁移，通过完整性/head 校验后原子安装 | 首装迁移失败时不创建/不保留半成品目标数据库 |
| `0001_m1_core` ～ `0022_history_scan_cancelled` 任一历史 revision | `0023_backup_policy` | 支持；22 个历史 revision 全量参数化矩阵均执行真实 Alembic upgrade 并验证业务探针保留 | 切换前创建一致性 `pre-upgrade` 快照；临时迁移失败不切换，切换后验证失败自动恢复快照 |
| 已是 `0023_backup_policy` | 同一 head | 支持；启动时 no-op，不额外制造升级快照 | 无需回滚 |
| 未来、未知、分叉或损坏 revision | 当前 head | 未声明支持；完整性/revision/临时迁移任一失败均失败关闭 | 不允许直接修改当前数据库；已有数据库保留原状态 |
| 当前数据库降级给旧镜像读取 | 旧 revision | 不支持原地 downgrade 作为生产回滚手段 | 必须恢复升级前备份，再启动与该备份兼容的旧镜像 |

自动化证据位于 `tests/integration/test_upgrade_matrix.py`。测试会从每个历史 revision 构造独立 SQLite 数据库，写入额外业务探针，然后走与 RuntimeManager 相同的安全升级入口，确认探针、Alembic head 和升级前安全快照都正确。

## 3. 运行时升级算法

PackBreaker 启动取得 `/config` 单实例锁并完成主密钥自检后，数据库升级采用以下顺序：

1. 读取并验证当前 SQLite 完整性与 revision；已是 head 时直接继续。
2. 对现有旧数据库先使用 SQLite Backup API 创建 `/config/backups/pre-upgrade` 一致性快照。
3. 在数据库同一文件系统创建 0600 临时文件；旧数据库升级时从快照复制，首次安装时从空临时数据库开始。
4. 只对临时副本执行 Alembic `upgrade head`，再执行 journal 规范化、`integrity_check` 和 revision=head 校验。
5. 全部通过后才 `os.replace()` 原子切换目标数据库，并再次校验。
6. 切换后验证失败时，现有数据库从 `pre-upgrade` 快照原子恢复；首次安装则删除失败目标。

因此迁移代码异常不会在当前生产数据库上“迁到一半”。主服务只有升级与 readiness 都通过后才进入工作状态。

## 4. 正式镜像升级基线与跨镜像门禁

`release-baseline.json` 当前固定最新正式 `v0.1.1`：tag、发布 commit、Alembic revision、Release workflow run 与公开 GHCR digest `sha256:76f4c041d1acecbb573cdbd49c45aec936bfd8cf3b263bc09153f7741f18e30d`。`scripts/validate_release_baseline.py` 已进入静态门禁，禁止把基线退化成可移动 tag、错误 digest、未来版本或与 tag 不一致的身份。正式 tag workflow 还会通过 GitHub Releases API 要求 baseline tag 必须等于当前最新正式 Release。

CI/container 与未来 tag release 都执行 `scripts/check-release-upgrade.sh`：先按基线 digest 启动上一正式镜像，在隔离 `/config` 写入合成兼容探针并创建一致性升级前备份；随后让当前候选镜像直接接管同一 config 并通过 readiness/探针校验；最后停止候选镜像，用**上一正式镜像自己的维护工具**恢复升级前备份，再启动同一基线 digest 并重新证明 readiness、Alembic revision 与探针数据。门禁明确禁止以 `alembic downgrade` 代替生产回滚。

正式 `v0.1.1` 的 Release workflow run `34924614659` 已在真实 `ubuntu-latest` Docker runner 上完成第一条真正跨版本证据：`v0.1.0@sha256:f7a396ac...d91fce` 启动并创建合成探针/备份，`v0.1.1` 候选接管同一 `/config` 后通过 readiness，随后用 `v0.1.0` 镜像恢复旧备份并再次通过 readiness/revision/探针校验。当前候选已推进到 `0.1.2`，因此下一次正式发布必须验证 `v0.1.1@sha256:76f4c041...18e30d → v0.1.2 候选 → 恢复 v0.1.1`。

`0.1.2` 候选同时新增运行时一键升级 helper：主服务只负责校验正式 Release、完整 preflight、在线一致性备份和受认证的 helper 请求；独立 helper 负责拉取目标 digest、停止/重建容器、切换瞬间静止数据库备份、Docker healthcheck 和失败回滚。该运行时链路不会替代 release workflow 的跨版本门禁，二者分别验证“发布前兼容性”和“用户现场执行路径”。
