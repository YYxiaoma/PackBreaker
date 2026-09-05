# PackBreaker

PackBreaker 是一个面向 PT 场景的自动拆包辅种系统。它以“大包”下载任务为输入，将内容识别为单影片或单集单元，跨站搜索匹配资源，并通过硬链接复用已有数据完成辅种。

## 核心能力

- 接入 qBittorrent 与 Transmission，支持多个下载器实例
- 监控大包任务并执行解析、搜索、匹配、硬链接、添加辅种和状态确认
- 通过统一适配器接入多个 PT 站点，首批计划支持 M-Team、HDTime 和 HHClub
- 提供失败重试、99% 卡死修复、缺失附属文件修复和人工确认
- 支持历史影片扫描辅种与电视剧按季、集拆包辅种
- 提供任务管理、配置、日志、通知和升级界面

## 技术方向

- 后端：Python 3.11、FastAPI、SQLAlchemy、Alembic、SQLite、APScheduler
- 前端：Vue 3、Vite、Element Plus、Pinia、Axios、ECharts
- 部署：Docker 单镜像，首个正式版本面向 `linux/amd64`

## 项目状态

项目当前处于需求确认与研发准备阶段，尚未发布可运行版本。

完整需求请参阅[需求基线 v0.3](./自动拆包辅种系统-需求基线-v0.3.html)。历史版本保留在[需求基线 v0.2](./自动拆包辅种系统-需求基线-v0.2.html)。

## 许可证

本项目采用 [MIT License](./LICENSE)。
