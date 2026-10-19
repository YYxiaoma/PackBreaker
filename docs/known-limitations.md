# 当前已知限制

用户已于 2026-10-18 确认七个新增站点的完整真实拆包/辅种任务已在本地环境通过验收，项目进度状态已记录为完成（见 [七站本地验收记录](v1.1.4-seven-site-local-acceptance.md)）。但仓库尚未据此生成或验证逐站机器可审计的 E2E 收据；因此不能把发布门禁中的证据缺口直接标成 `COMPLETE_REAL_E2E`。以下条目记录历史限制与证据边界；相关现场验证议题已按用户决定移出当前待办，并非当前承诺执行的工作。

- CarPT 独立受控 COPY、journal、Transmission ADD/VERIFY/START 的全新 PackBreaker execution 机器回执尚未在仓库中补齐；旧只读 execution 不得解冻。此项是审计证据边界，不否定用户已确认的本地任务验收。
- 历史现场记录曾出现站点网络、搜索与详情超时、限流，以及 HDDolby、SpringSunday 的间歇性问题；本轮未连接真实站点复测当前是否仍复现。
- 历史记录列出 BTSCHOOL、Rousi Pro、UBits 等 Tracker announce 异常；本轮未采集新的 Tracker 日志，不能断言故障当前仍在发生或已经修复。
- 下载器失联、校验与启动超时、辅助文件补齐、自动重试上限及 journal 恢复仍属于历史现场证据覆盖范围；本轮未对真实下载器执行验证。
- Hardlink 跨文件系统不可用；共享 inode 的目标必须在可能下载写入前完成隔离。
- Web 在线升级需要 Docker socket 权限，且不会自动改写宿主机 Compose 镜像声明。

现场验证议题 [Issue #33](https://github.com/YYxiaoma/PackBreaker/issues/33) 已按用户决定以 `not_planned` 关闭；[Issue #5](https://github.com/YYxiaoma/PackBreaker/issues/5) 记录了移出待办的决定。这些条目仍是已知限制，但不是当前承诺执行的现场任务，也不表示相关故障已修复。若未来重新开展，需另行明确范围与真实环境授权。历史风险接受记录见 [审计评论](https://github.com/YYxiaoma/PackBreaker/issues/5#issuecomment-6164915759)。其他使用与维护说明见 [项目说明](README.md)。
