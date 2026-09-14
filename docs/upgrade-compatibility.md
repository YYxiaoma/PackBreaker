# 数据库升级兼容矩阵

## 1. 当前基线

当前项目版本为 `0.1.0`，Alembic head 为 `0023_backup_policy`。升级兼容性以数据库 revision 和可恢复证据为准，不以镜像标签名称猜测。

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

## 4. 尚未取得的发布级证据

仓库目前尚无早于 `0.1.0` 的正式 PackBreaker 发布镜像，因此“上一正式 release 镜像 + 该 release 真实匿名化数据库 → 新 release 镜像”的跨镜像矩阵还没有历史对象可测。首个正式发布后，每次发布必须保留前一 release 的匿名化/合成兼容数据库夹具或可重建脚本，并在目标 Docker 环境执行升级、readiness、失败恢复旧备份与旧镜像的完整演练。
