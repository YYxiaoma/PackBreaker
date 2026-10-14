# 当前支持边界

功能、部署和维护入口见 [项目说明](README.md)。站点资格由 `backend/app/domain/site_config.py` 中的 `SITE_PROFILE_REGISTRY` 与运行时配置共同决定。

当前注册站点：M-Team、HDTime、HHClub、KeepFrds、HDHome、UBits、HDFans、BTSCHOOL、PTTime、Rousi Pro、聆音、PTerClub、Audiences、SpringSunday、HDDolby、U2、不可躺（TANGPT）、CarPT。

PTerClub、Audiences、SpringSunday、HDDolby、U2、TANGPT、CarPT 的代码适配状态为 `SUPPORTED`，正式完整真实 E2E 收据仍为 **0/7**；验收清单中的 `PENDING_REAL_VALIDATION` 表示证据缺口，不代表代码适配仍被禁用。v1.1.2 已按用户明确接受未完成实测风险的策略发布。

下载器包括 qBittorrent 与 Transmission；具体可用动作以版本、能力探测、路径映射和实时执行门禁为准。提供 linux/amd64 与 linux/arm64 镜像。连接测试、子链测试与完整 PackBreaker execution 验收应分别报告。
