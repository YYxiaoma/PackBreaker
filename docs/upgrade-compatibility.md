# 数据库升级兼容矩阵

## 1. 当前基线

当前项目候选版本为 `1.0.0`，Alembic head 为 `0029_v018_compatibility`；上一正式版本基线为 `v0.1.9`。升级兼容性以数据库 revision 和可恢复证据为准，不以镜像标签名称猜测。

## 2. 自动化兼容矩阵

| 来源状态 | 目标 | 自动化结论 | 回滚边界 |
| --- | --- | --- | --- |
| 空配置、无数据库 | 当前 head | 支持；先在同文件系统临时数据库执行全量迁移，通过完整性/head 校验后原子安装 | 首装迁移失败时不创建/不保留半成品目标数据库 |
| `0001_m1_core` ～ `0028_telegram_approval_v018` 任一历史 revision | `0029_v018_compatibility` | 支持；历史 revision 全量参数化矩阵均执行真实 Alembic upgrade 并验证业务探针保留 | 切换前创建一致性 `pre-upgrade` 快照；临时迁移失败不切换，切换后验证失败自动恢复快照 |
| 已是 `0029_v018_compatibility` | 同一 head | 支持；启动时 no-op，不额外制造升级快照 | 无需回滚 |
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

`release-baseline.json` 当前固定最新正式 `v1.0.0`：tag、发布 commit `ce86939224301315092b27621830560bf8438d58`、Alembic revision `0029_v018_compatibility`（本版没有新增迁移）、成功恢复发布 workflow run `35507181886` 与公开 GHCR 双架构 index digest `sha256:fce1f3e3c0f56a8c024bbbf46a65fbc4ca51225a5c2bc2543fdddbb02c21a0c4`。`scripts/validate_release_baseline.py` 已进入静态门禁，禁止把基线退化成可移动 tag、错误 digest、未来版本或与 tag 不一致的身份。正式 tag workflow 还会通过 GitHub Releases API 要求 baseline tag 必须等于当前最新正式 Release。

CI/container 与未来 tag release 都执行 `scripts/check-release-upgrade.sh`：先按基线 digest 启动上一正式镜像，在隔离 `/config` 写入合成兼容探针并创建一致性升级前备份；随后让当前候选镜像直接接管同一 config 并通过 readiness/探针校验；最后停止候选镜像，用**上一正式镜像自己的维护工具**恢复升级前备份，再启动同一基线 digest 并重新证明 readiness、Alembic revision 与探针数据。门禁明确禁止以 `alembic downgrade` 代替生产回滚。

正式 `v0.1.2` 的 Release workflow run `34937718889` 已在真实 `ubuntu-latest` Docker runner 上完成跨版本证据：`v0.1.1@sha256:76f4c041...18e30d` 启动并创建合成探针/备份，`v0.1.2` 候选接管同一 `/config` 后通过 readiness，随后用 `v0.1.1` 镜像恢复旧备份并再次通过 readiness/revision/探针校验；同一正式发布流水线还运行独立 updater helper 的真实 digest pull、容器替换，以及故障候选修改数据库后自动恢复旧数据库/旧容器的 E2E。

`v0.1.2` 已正式提供运行时一键升级 helper：主服务只负责校验正式 Release、完整 preflight、在线一致性备份和受认证的 helper 请求；独立 helper 负责拉取目标 digest、停止/重建容器、切换瞬间静止数据库备份、Docker healthcheck 和失败回滚。该运行时链路不会替代 release workflow 的跨版本门禁，二者分别验证“发布前兼容性”和“用户现场执行路径”。

`v0.1.4` 起引入的“单常驻 PackBreaker + 一次性 helper”路径已在后续版本持续演进；`v0.1.6` Release workflow run `35297829243` 已在真实 GitHub Actions Docker runner 上完成该 transient helper 的容器替换、docker.sock 保留、数据库探针、终态持久化和 helper 自动清理。首次 v0.1.6 发布尝试还通过门禁暴露了“新容器已 healthy，但 transient helper 尚未完成最后终态写入”的 E2E 竞态；脚本改为有界等待 `succeeded` 终态并对 `rolled_back/failed/manual_recovery_required` 继续失败关闭后，正式 Release 全链路通过。

`v0.1.7` Release workflow run `35302608582` 进一步以正式 `v0.1.6` baseline 完成 `v0.1.6 → v0.1.7 → v0.1.6` 相邻版本门禁，并在真实 Docker updater E2E 中验证 Compose labels 与 docker.sock 在 transient replacement 后保留。该能力因此从 candidate 证据升级为正式发布证据。

`v0.1.8` Release workflow run `35366864779` 以正式 `v0.1.7` baseline 完成 `v0.1.7 → v0.1.8 → v0.1.7` 相邻版本门禁、真实 updater helper 升级与自动回滚，并在全部门禁通过后发布不可变 linux/amd64 镜像、SBOM、release manifest 与 GitHub Release。正式 digest 为 `sha256:f114296a40c3bc68f036071382ea3029818fc909ef78a2c07a5ab382e6c4d0d3`。

`v0.1.9` Release workflow run `35429394091` 以正式 `v0.1.8` baseline 完成 `v0.1.8 → v0.1.9 → v0.1.8` 相邻版本门禁、真实 updater helper 升级与自动回滚，并在全部门禁通过后发布不可变 linux/amd64 镜像、SBOM、release manifest 与 GitHub Release。正式 digest 为 `sha256:3bf7eee3825821a5fef28338b7f1ce749cbae353bcd3a4ef81883ee6a021261d`。

`v1.0.0` 提交 `90572a9` 的 [Candidate Docker E2E run `35503783376`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35503783376) 在独立 Docker Runner 上实际通过 v0.1.9 正式 GHCR 不可变 AMD64 基线身份核对、隔离启动、`v0.1.9 → 本地 v1.0.0 候选 → v0.1.9` 升级回滚，以及 updater/Compose E2E；[CI run `35503783297`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35503783297) 的原生 ARM64 合成基线升级回滚/跨镜像恢复亦通过。这些证据不代表使用**正式发布后 v1.0.0 GHCR 不可变 digest**完成 AMD64 相邻升级回滚；该门禁已配置在新镜像推送后、发布资产生成前执行，须取得实际正式 Release run 结果后才能将其标为通过。

**v1.0.0 正式已发布摘要的升级与回滚（2026-09-20）**：[恢复发布 run `35507181886`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35507181886) 使用 v0.1.9 正式 AMD64 digest 与已经推送的 v1.0.0 index digest `sha256:fce1f3e3c0f56a8c024bbbf46a65fbc4ca51225a5c2bc2543fdddbb02c21a0c4`，完成 `v0.1.9 → v1.0.0 → v0.1.9` 的真实 Docker 合成数据升级、升级前备份恢复及旧镜像 readiness；同时，独立 QEMU ARM64 和原生 ARM64 Runner 对同一 index digest 的运行、预检和数据库备份均通过。该完整发布已生成并回读正式 GitHub Release 资产，GHCR `stable/latest` 指向 v1.0.0；没有正式 v0.1.9 ARM64 镜像，因此不声称旧版 ARM64→新版 ARM64 的跨版本升级。

v1.0.0 ARM64 研发阶段另设原生 ARM64 合成双镜像数据库备份/恢复门禁：由同一候选构建带合成标签但 image ID 不同的本地 ARM64 基线，严格要求两镜像应用版本一致，分别启动并核查持久化数据库探针、revision、备份恢复与最终 readiness。它用于补齐原生 ARM64 容器文件/数据库链路，不替代正式 v0.1.9（仅 AMD64）→ v1.0.0 AMD64 的相邻版本门禁，不表示旧版已正式发布 ARM64 镜像，也不构成正式 ARM64 跨版本升级证据。
