# 测试与验收

当前命令和分层说明见 [项目说明](README.md)。默认测试使用合成数据和隔离目录，不访问真实站点、下载器或媒体。

## 核心验证

- `uv run python scripts/check.py`：仓库安全扫描、验收索引、发布清单、基线、静态检查和 API 契约。
- `uv run python scripts/test.py`：pytest、Vitest 与前端构建。
- UI 交互使用 `node scripts/check-prototype.cjs`、`node scripts/check-unpack-v2-ui.cjs` 对应的隔离浏览器流程。
- Docker、升级回滚和双架构运行证据由 GitHub Actions 的 CI / Candidate Docker E2E / Release 提供。七站完整真实拆包/辅种任务已由用户确认在本地环境通过验收，记录见 [七站本地验收记录](v1.1.4-seven-site-local-acceptance.md)；自动化检查与真实环境验收分别记录，不互相替代。

## 自动化回归验收条目

- 重复触发 10 次结果唯一。
- LINKING、ADDING、CLIENT_VERIFYING 故障注入后恢复正确。
- execution plan 必须绑定明确的目标下载器 version/能力摘要/远端 save path
- ADDING 覆盖 qB 响应丢失与“journal 已 APPLIED、task 状态尚未提交”两个崩溃点
- CLIENT_VERIFYING 必须以独立 recheck journal + 单步状态 tick 工作
- SEEDING 必须以独立 start journal 驱动
- 启动恢复必须有界扫描
- 取消/回滚必须证明 qB 与文件资源都属于当前 task/execution plan
- `DONE` 是不可逆终态
- 副作用开始前取消必须覆盖
- 数据拆包 v2 的公开写动作必须要求管理会话 CSRF；人工审核还必须要求 `If-Match` + `Idempotency-Key`，重试动作要求 `If-Match`。
- 浏览器门禁必须覆盖公开动作 UI
- 数据拆包 v2 前端公开动作控制必须覆盖：保存与执行分离；仅 `review_allowed=true` 的影片显示“审核”；匹配超时 / 匹配错误只暴露 item 级“重试”；审核弹窗默认选中最高候选但不自动提交；全部可见业务状态保持中文。
- 数据拆包 v2 不再暴露旧 TaskEvent/SSE 接口；执行详情由 execution + items 分页快照提供，支持稳定 item 游标、状态筛选和六阶段中文管线；终态通过通知 outbox 幂等投影，不依赖浏览器保持 EventSource 连接。
- operation journal → TaskEvent 投影必须与 journal insert/CAS 同事务
- operation journal 对账接口必须验证响应字段白名单
- operation journal retention 必须覆盖 preview 与写时双重证明
- 数据拆包 v2 不得暴露旧任务的 retention/purge 前端入口；历史 journal 清理继续在后端 fail-closed 并支持同键重放
- 站点管理前端必须直接使用真实 `/sites` 契约而非本地演示状态
- 源文件在所有验收场景中内容与 inode 不变。
- 数据库、配置导出、日志、通知和诊断包无可用明文凭证。
- 备份、迁移、升级健康检查和失败回滚演练通过。
