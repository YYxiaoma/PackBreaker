# 当前已知限制

v1.1.2 已正式发布，但七站完整真实 E2E 收据仍为 **0/7**。用户已明确接受该实测缺口，发布不代表七站完整链路均成功。

- CarPT 独立受控 COPY、journal、Transmission ADD/VERIFY/START 的全新 PackBreaker execution 完整回执尚缺；旧只读 execution 不得解冻。
- 站点网络、搜索与详情超时、限流以及 HDDolby、SpringSunday 的间歇性问题仍需现场跟踪。
- BTSCHOOL、Rousi Pro、UBits 等 Tracker announce 故障待持续排查。
- 下载器失联、校验与启动超时、辅助文件补齐、自动重试上限及 journal 恢复仍需现场验证；已有自动化不能替代该证据。
- Hardlink 跨文件系统不可用；共享 inode 的目标必须在可能下载写入前完成隔离。
- Web 在线升级需要 Docker socket 权限，且不会自动改写宿主机 Compose 镜像声明。

后续现场验证见 [Issue #33](https://github.com/YYxiaoma/PackBreaker/issues/33)。风险接受记录见 [审计评论](https://github.com/YYxiaoma/PackBreaker/issues/5#issuecomment-6164915759)。其他使用与维护说明见 [项目说明](README.md)。
