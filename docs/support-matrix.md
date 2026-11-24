# 当前支持边界

功能、部署和维护入口见 [项目说明](README.md)。站点资格由 `backend/app/domain/site_config.py` 中的 `SITE_PROFILE_REGISTRY` 与运行时配置共同决定。

当前注册站点共 117 个：M-Team、HDTime、HHClub、KeepFrds、HDHome、UBits、HDFans、BTSCHOOL、PTTime、Rousi Pro、聆音、PTerClub、Audiences、SpringSunday、HDDolby、U2、不可躺（TANGPT）、CarPT、OurBits，以及其余 98 个来自 [PT-depiler](https://github.com/pt-plugins/PT-depiler) 站点定义的私有站点。全部站点均已开放配置、启用与正式任务入口。

PTerClub、Audiences、SpringSunday、HDDolby、U2、TANGPT、CarPT 的代码适配状态为 `SUPPORTED`。用户已确认七站完整真实拆包/辅种任务在本地环境全部通过验收，记录见 [七站本地验收记录](v1.1.4-seven-site-local-acceptance.md)。

下载器包括 qBittorrent 与 Transmission；具体可用动作以版本、能力探测、路径映射和实时执行门禁为准。提供 linux/amd64 与 linux/arm64 镜像。连接测试、子链测试与完整 PackBreaker execution 验收应分别报告。

按用户要求（v1.1.18），PT-depiler 来源的 99 个站点已取消全部准入限制：`support_status` 统一置为 `SUPPORTED` 并全部纳入 `PERSISTED_SITE_KINDS`，可创建、可启用、可进入分析搜索与拆包/辅种任务，CookieCloud 同步也会自动导入。这些站点尚未完成真实只读探测与完整拆包/辅种验收，因此本文件不为其真实链路作任何通过性声明；实际可用性以站点返回结果为准，发现问题应回退为待验证状态。

其中 HDArea、HDCity、RetroFlix、CSPT、海胆已按上游定义建立专用适配器（API/Torznab、自定义列表与详情页、isapi JSON），其余站点走通用 NexusPHP 解析；通用适配器仍统一关闭站点侧逐种子做种列表（接口未证实时返回不支持，而不是给出错误结论），IMDb/豆瓣搜索能力按各站 Profile 声明。
