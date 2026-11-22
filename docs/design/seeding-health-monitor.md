# 监控中心 · 做种健康巡查 开发文档

状态：**待评审**（v0.9，2026-11-20）
作者：开发助手
范围：新增「监控中心」一级菜单 + 「做种健康巡查」功能；不含 H&R 看板、站点资产趋势、优惠窗口等其他监控能力
参考：草图 A（宽屏列表版）、草图 B（详情抽屉版）

---

## 0. 一页结论

| 问题 | 结论 |
| --- | --- |
| 数据从哪来 | 下载器侧为主（`list_torrents` + trackers），站点侧为辅（`fetch_details` 404 判定 / 站点做种列表），本地文件系统为补充（`stat`） |
| 实时还是定时 | **定时巡查 + 页面读库**。默认每 6 小时一次，可配 1–24 小时；页面不实时跟踪，只展示最近一次结果 |
| 「立即巡查」是什么 | 手动触发一次同步巡查（异步任务 + 进度轮询），不改变定时计划 |
| 最大技术风险 | `GET /sites/{id}/seeding` 是桩实现、`SiteAdapter` 无 `list_seeding` 能力 → v1 先不做该类检测；但请求路径与表格解析器已存在，v1.1 可低成本补齐（§3.4） |
| v1 异常类型范围 | 3 类：`SITE_MISSING`、`TRACKER_ERROR`、`FILE_MISSING`；`SITE_ONLY` 留到 v1.1 |
| 最大产品风险 | 站点临时故障会被误判成「站侧删种」，必须用连续确认 + 站点级降级两道闸门 |
| 建议工期 | M0 前置 5 天，M1 后端 6 天，M2 前端 5 天，M3 打磨 3 天 |

### 0.1 实现状态（2026-11-20）

| 里程碑 | 状态 | 内容 |
| --- | --- | --- |
| M0 适配层 | 已完成 | `DownloaderTorrent` 增加 `seeding_time_seconds` / `added_at` / `tracker_state` / `tracker_message` / `tracker_last_announce_at`；qB 增加 trackers 读取；TR 扩 `secondsSeeding` / `addedDate` / `trackerStats`；两侧新增 `export_torrent` 供身份解析 |
| M1 后端 | 已完成 | 迁移 `0057`、`domain/seeding_health.py`、`domain/torrent_identity.py`、`SeedingHealthService`、`SeedingHealthDriver`、8 个接口 |
| M2 前端 | 已完成 | 监控中心一级菜单 + 做种健康巡查页 + 详情抽屉 |
| v1.1 | 未开始 | `SITE_ONLY` 检测与 NexusPHP 列表解析 |

**实现与设计的偏差（评审需确认）**

1. **修复动作只开放「强制重校验」**。`RETRIEVE_TORRENT` / `RESEED` 当前返回 `409 SEEDING_HEALTH_ACTION_MANUAL`，明确提示人工处理，避免半成品写入。
2. **H&R 规则不内置默认值**。未配置的站点 `hnr_remaining_hours` 落库为 NULL，页面显示「—」，绝不按 0 风险计入。规则通过 `PUT /policies/{site_key}` 按站点配置。
3. **身份解析只实现 comment 一条路径**。`identity_locked` 字段已预留人工绑定；执行计划（PLAN）与名称匹配留到 v1.1。
4. **表结构比初版多 5 列**：`site_outcome`、`site_checked_at`、`consecutive_misses`、`tracker_checked_at`、`hnr_remaining_hours`，用于探测缓存、连续确认与 H&R 汇总。
5. **`SITE_NOT_FOUND` 只在 NexusPHP 适配器落地**。`_raise_for_status` 把 404 独立成码；M-Team / Rousi Pro 仍返回 `SITE_HTTP_ERROR`，这两个站的 `SITE_MISSING` 暂时恒为 UNKNOWN（宁可漏报）。
6. **不做 WebSocket 与实时轮询**。除「立即巡查」后的进度轮询外，页面每 30 秒仅刷新 summary。

### 0.2 真实规模验证与调优（7255 个做种）

在开发机真实库（TR 7255 + QB 1 个做种、17 个站点）跑通后，发现三处设计与现实不符，已修正：

| 问题 | 现象 | 修正 |
| --- | --- | --- |
| 身份解析依赖导出 .torrent | Transmission 4.1.3 的 `torrent-get` **不返回 `torrentFile`**，解析率 0 | 改为直接读列表接口的 `comment` 字段（qB/TR 都有），零额外请求，一次巡查解析出 4286 个（59%，其余种子本身没有站点链接） |
| 站点侧逐种子探测不可行 | 7255 个种子按 24h TTL 轮转，每轮需 ~1800 次详情页请求；M-Team 单请求间隔 90 秒 | 改为**仅对 `tracker_state == ERROR` 的种子**做站点确认（删种最可靠的前兆），并按站点请求间隔分档限流（≥60s → 8 个/站/轮） |
| 单轮超时丢失进度 | 超时后整轮结果不落库，下一轮从头再来 | 每个站点探测完立即落库；超时降级为 PARTIAL，进度保留 |
| 5000 条 watch 上限 | 真实库 7255 个，摘要与实际覆盖不一致 | `max_watches` 提到 20000，摘要与判定口径一致 |
| 同一缺失目录放大成 13 条异常 | 「九品芝麻官」目录缺失影响 13 个同片种子 | FILE_MISSING 按 `content_path` 聚合，只报一条并标注「影响 N 个种子」，历史重复自动 RESOLVED |
| 进程被强杀留下 RUNNING 记录 | 页面永远显示「巡查中」 | 新一轮开始前把残留 RUNNING 收尾为 CANCELLED |

真实库首轮结果：7252 个种子完成检查，命中 2 个缺失路径（14 条历史异常收敛为 2 条），全程约 40 秒；无 tracker 报错，因此站点侧未产生探测请求。

### 0.3 上线后反馈修复（同日）

| 反馈 | 根因 | 修复 |
| --- | --- | --- |
| 手动添加候选时 Rousi Pro 的详情链接被拒 | 校验只认 `details.php?id=` 一种形态；M-Team 用 `/detail/`、Rousi Pro 用 `/torrents/` | 新增 `domain/site_detail_url.py` 作为唯一真源，前后端共用；页面「打开详情页」生成的链接也改为按站点类型生成（此前对 M-Team/Rousi 生成的链接自己都校验不过） |
| 筛选条件竖排 | Element Plus 的 `el-select` 默认撑满容器，`flex-wrap` 下每项独占一行 | 固定宽度 148px + `flex: 0 0 auto` |
| 7252 个种子只有 2 条异常，怀疑漏检 | ① TR 4.1.3 的 `torrent_get` 不返回 `trackerStats`，而站点核对以「tracker 报错」为门槛，条件永不成立 ② 767 个种子的路径前缀（`/downloads/`）没有任何映射规则，被静默跳过 | ① 站点核对改为**默认关闭 + 按站点间隔抽样轮转**，不再依赖 tracker 字段 ② summary 增加 `coverage` 与 `unmapped` 计数，页面对未映射种子出黄色警告 |

**新增判据**：任何依赖下载器返回某字段的判断，必须先在真机验证该字段确实存在——Transmission 4.1.3 会静默丢弃未知的 `fields`，不报错。

### 0.4 路径映射补齐后的判定修正

补齐 TR 的 `/downloads → /mnt/nas2/downloads` 后，覆盖率达到 7254/7254，同时暴露出判定缺陷：

原规则是「父目录不存在 → 记为不可见（UNKNOWN）」，本意是防止磁盘没挂载时误报。但真实场景里更常见的是**整个子目录被删**（下完的种子目录被移走/清理），此时父目录同样不存在，于是被静默放过。实测 23 个缺失路径只报出 2 条。

修正为**以映射的挂载根作为可见性锚点**：

| 情况 | 判定 |
| --- | --- |
| 路径存在 | 正常 |
| 挂载根不存在（盘没挂上） | UNKNOWN，不报 |
| 挂载根存在但路径不存在 | **缺失**（含中间目录被删） |

修正后同一次巡查报出 5 条异常、覆盖 23 个种子，与独立脚本枚举的结果完全一致。

### 0.5 后续补齐（同日 17:20）

| 项 | 实现 |
| --- | --- |
| H&R 策略页面入口 | 巡查页新增「巡查设置」弹窗，按站点配置 `min_seeding_hours`；留空显示「规则未知」，不计入 H&R 风险。保存后下一轮巡查重算风险分 |
| 站点侧核对开关 | 迁移 `0058` 新增 `seeding_health_config`（无默认行，未配置时沿用环境变量）；`GET/PUT /monitor/seeding-health/settings` 支持运行时切换，页面开关即时生效于下一轮 |
| M-Team 404 | `_raise_for_http_status` 对 404 抛 `SITE_NOT_FOUND`，与 NexusPHP 对齐，M-Team 的 `SITE_MISSING` 可正常判定 |

**仍未落地**：Rousi Pro 的 `fetch_details` 本身返回 `SITE_ADAPTER_PENDING`（站点适配未完成契约验收），给它的 404 做映射没有意义，因此该站的 `SITE_MISSING` 仍是 UNKNOWN。此外 `SITE_ONLY` 检测、`RETRIEVE_TORRENT` / `RESEED` 两个修复动作仍按 v1.1 范围留待单独排期。

### 0.6 SITE_ONLY 检测（v1.1 已完成）

| 组件 | 实现 |
| --- | --- |
| 站点列表能力 | `domain/site_adapter.py` 新增 `SiteSeedingItem` / `SiteSeedingList`；`list_seeding()` 是**可选能力**（独立 Protocol），不进主 `SiteAdapter`，避免 37 个测试假适配器与不支持该能力的站点被迫实现 |
| NexusPHP 适配 | 复用已生产验证的 `getusertorrentlistajax.php` 请求路径；从现成 `_NexusHtmlParser.rows` 提取 `details.php?id=数字`、标题与大小；page 参数 0 基；一次开发覆盖 16 站。M-Team / Rousi 返回 `supported=False` |
| 对外接口 | `/sites/{id}/seeding` 从硬编码空列表改为真实调用，支持 `cursor` 翻页 |
| 巡查对账 | `_detect_site_only()` 拉取站点全列表，与 `seeding_watch` 的 `(site_key, remote_torrent_id)` 做差集；迁移 `0059` 允许 `downloader_id=NULL` 的虚拟 watch（`presence=SITE_ONLY`）以复用现有 issue 表与抽屉 |

**覆盖率闸门（真实数据倒逼）**：首次实跑 HDTime 报出 546 条 `SITE_ONLY`，实为误报——本地该站身份识别数 **0**（种子 comment 里没有站点链接），差集等于「未识别身份」而非「站点独有」。因此判定前强制要求：

- 站点列表必须完整取回（翻页到 `next_cursor=None`，或累计达到 `total_hint`；最多 40 页）
- 本地该站识别数必须 ≥ 站点列表数的 80%，且不能为 0
- 任一条件不满足即跳过该站并记录 WARNING，宁可漏报
- 站点侧已消失的历史 `SITE_ONLY` 异常在下一轮自动恢复

修复过程中还发现并修正了虚拟 watch 未 `flush()` 就写 issue 外键导致整轮 FAILED 的问题。

### 0.7 修复动作闭环（v1.1 已完成）

`RETRIEVE_TORRENT` / `RESEED` 不再是 409 占位，改为两段式：

1. **预览** `POST /issues/{id}/repair-preview`（只读）：按 `site_key + remote_torrent_id` 从站点拉 `.torrent`，解析出名称/大小/文件数/v1+v2 hash/metainfo digest/payload sha256，选定目标下载器与 `save_path`，返回摘要值和风险提示。不写任何库、不发任何外部写操作。
2. **执行** `POST /issues/{id}/repair`：必须回传预览给出的 `expected_payload_sha256`；不一致立即 409（站点内容已变化，需重新预览）。随后：
   - `BEGIN IMMEDIATE` + `idempotency_key=(issue_id, operation, sha256)` 写 intent（迁移 `0060` 的 `seeding_repair_journal`，复用 `OperationStatus` 状态机）
   - 以 `paused=True`、`skip_checking=False`、`verification_level=CLIENT_CHECK_REQUIRED` 调用下载器写适配器
   - 回读 `get_torrents` 确认：命中记 `APPLIED`，未命中/异常记 `RECONCILE_REQUIRED`，绝不盲目重发
   - 重复提交命中唯一键直接返回原结果

**安全取舍**：不复用 unpack 的 `unpack_external_operation_journal`（其 `item_id` 外键绑定 unpack item），但完整复用它的意图先行 / 幂等键 / 版本围栏 / 不确定即对账模式。因为无法证明本地内容与 torrent 逐字节一致，**禁止 skip_checking**；加入的任务保持暂停，需人工校验后启动。

迁移 `0059` 同时保留了 SITE_ONLY 虚拟 watch 的 `downloader_id=NULL` 能力。

### 0.8 项目 review 后的修复（同日 21:00）

四路独立审查（后端架构/前端契约/数据库迁移/测试工程）发现的问题中，已修复以下：

| 级别 | 问题 | 修复 |
| --- | --- | --- |
| P0 | `SeedingIssueRowResponse.downloader_id: str` 拒绝 None，SITE_ONLY 一旦出现 `/issues` 必 500 | 改为 `str \| None`，重生成契约；前端标签函数接受可空并显示「—」 |
| P1 | 详情抽屉 `??` 与 `===` 优先级写错，所有证据都显示同一句 SITE_ONLY 文案 | 改为先判断 `site.result`，再回退 `site.detail` |
| P1 | SITE_ONLY 种子被加回下载器后异常永不恢复 | 清理条件加入「本地已有该 ID」；并修正「差集为空直接 continue」导致清理根本不执行的第二处缺陷 |
| P1 | summary 的 `healthy` 减去分母外的 SITE_ONLY 异常 | `watched` 改为 ACTIVE + SITE_ONLY watch 数 |
| P1 | 修复动作 intent 崩溃后无恢复、`RECONCILE_REQUIRED` 终态死胡同 | 幂等命中时按 intent 回读恢复；`get_torrents` 有限重试 3×300ms；幂等键纳入 `detect_count` 使复发异常可再次修复 |
| P1 | 手动巡查同步阻塞 40s~20min，202 契约破裂 | `start_once()` 先建 RUNNING 记录再后台执行；忙时稳定 409 `SEEDING_HEALTH_RUN_ACTIVE`；孤儿收尾只处理超过超时的记录 |
| P1 | 前端快速切换 issue 时旧预览挂到新 issue | 请求序号 + `props.id` 校验 + id 变化时清空预览；列表筛选与轮询同样防旧响应/卸载后继续 |

测试侧：fake SQLite 现在走 `configure_sqlite_engine` 与生产同语义（外键/PRAGMA），新增「种子加回下载器后 SITE_ONLY 自动恢复」回归用例。


---

## 1. 现状核查（已逐条验证，不是推测）

| # | 事实 | 位置 | 影响 |
| --- | --- | --- | --- |
| 1 | `/sites/{site_id}/seeding` 硬编码返回 `supported=False, items=[]` | `backend/app/api/sites.py:686` | 站点侧做种列表不可用 |
| 2 | `SiteAdapter` 只有 `capabilities / test_connection / fetch_user_profile / search / fetch_details / fetch_torrent` | `backend/app/domain/site_adapter.py:125` | 需新增 `list_seeding` 能力 |
| 3 | `DownloaderTorrent` 只有 hash/name/status/progress/size/category/tags/tracker/save_path/content_path | `backend/app/infrastructure/adapters/downloaders.py:54` | 缺 `seeding_time`、缺 tracker 错误详情、缺 `added_on` |
| 4 | qB 只调 `/api/v2/torrents/info`，未调 `/trackers` | `adapters/downloaders.py:524` | Tracker 异常类型无数据来源 |
| 5 | Transmission 的 `torrent_get` fields 只有 7 个，未取 `secondsSeeding` / `trackerStats` | `adapters/downloaders.py:1016` | 同 4，但 TR 扩展成本极低 |
| 6 | 已有 cron 驱动范式：`SiteStatisticsDriver`（03:37 每日、tick_lock、start/stop 注册于 lifespan） | `application/site_statistics_driver.py`、`main.py:317/518/531` | 新驱动可直接复用该范式 |
| 7 | 前端为 hash 路由，导航唯一真源是 `navigation.ts` | `frontend/src/navigation.ts` | 新增一级菜单需改 6 处映射 |
| 8 | 最新迁移为 `0056_movie_dedup_shared_link_bytes` | `backend/migrations/versions/` | 新迁移编号 `0057` |
| 9 | 已有从 torrent comment 反解站点详情页链接的成熟做法 | `scripts/build_template_from_transmission.py` | 可直接产品化为「种子身份解析」 |
| 10 | 站内通知只有 `record_version_update` 一个写入口 | `application/admin_notifications.py:68` | 需新增通用 `record()` |
| 11 | `fetch_user_profile` **已经在请求** `/getusertorrentlist.php`（PTTime）或 `/getusertorrentlistajax.php`（其余），参数含 `type=seeding`，但只解析「条数 + 总大小」 | `adapters/nexusphp.py:796-843` | 站点列表**请求能力已具备**，缺逐行解析 |
| 12 | `_NexusHtmlParser` 已把表格解析为 `rows`（`_Row` 列表），`_parse_nexus_ajax_seeding_summary` 未使用行内明细 | `adapters/nexusphp.py:227`、`:2485` | 明细解析器可直接基于现成 `rows` 实现 |
| 13 | 18 个站中 16 个共用 `NexusPhpWebAdapter`（Profile 参数化），仅 M-Team / Rousi Pro 为独立 API 实现 | `adapters/sites.py`、`adapters/nexusphp.py:89` | 站点列表适配是「1 次 + 16 站生效」，不是 16 次 |

---

## 2. 草图差异裁决（评审重点）

两张草图信息架构不同，必须先定，否则前端返工。

| 维度 | 草图 A（宽屏） | 草图 B（抽屉） | 本方案采用 |
| --- | --- | --- | --- |
| 一级菜单 | 总览 / 任务中心 / 配置中心 / 系统管理 / 通知收件箱 | 首页 / 做种巡查 / 异常列表 / 站点管理 / 下载器管理 / 种子管理 / 任务调度 / 系统设置 | **只新增「监控中心」一项**，插在「任务中心」之后；现有四组及其全部子项零改动 |
| 做种巡查位置 | 任务中心子项（面包屑为「任务中心 / 做种健康巡查」） | 一级菜单 | **改为「监控中心 / 做种健康巡查」**，与用户诉求一致 |
| 异常列表 | 页面内类型 tab + 筛选 | 独立二级菜单 | **页面内 tab**，不做二级菜单，避免 IA 分裂 |
| 详情形态 | 行内「查看」 | 右侧抽屉 + 证据时间线 | **采用 B 的抽屉**，信息密度明显更好 |
| 指标卡 | 巡查种子 / 正常做种 / 异常种子 / H&R 风险 | 当前做种 / 异常种子 / 待处理 / 已修复 | **采用 A 的四卡**，并吸收 B 的「待处理」作为第五个可选指标 |
| 修复动作 | 「一键修复」单一按钮 | 三个具体动作 + 推荐标注 | **采用 B 的三动作**，主按钮为推荐项 |

**草图 B 一处口径错误，需修正**：其「已修复 1,260」等于「正常做种 1,260」，把「当前正常」当成了「已修复」。本方案定义为「**已恢复**（本次巡查中从异常转为正常的实例数）」，初始值应为 0，不是 1260。

---

## 3. 数据来源矩阵（核心章节）

这是本功能最容易被做错的地方。四类异常的数据来源、判定逻辑、可信度完全不同，必须分开设计。

### 3.1 四类异常

| 异常类型 | 业务含义 | 判定依据 | 数据来源 | 当前可用性 |
| --- | --- | --- | --- | --- |
| `SITE_MISSING` 站侧无记录 | 下载器在做种，但站点已经查不到这个种子 | 站点详情页连续 N 次返回 404 / 不存在 | `SiteAdapter.fetch_details(torrent_id)` | ✅ 可用（需先解析出 torrent_id） |
| `SITE_ONLY` 站侧有但下载器没有 | 站点显示我在做种，下载器里没有对应任务 | 站点做种列表 ∖ 下载器种子列表 | `SiteAdapter.list_seeding()` | ❌ **v1 不启用**；v1.1 用 NexusPHP 基类一次覆盖 16 站（见 §3.4–3.6） |
| `TRACKER_ERROR` Tracker 持续报错 | tracker 返回 unregistered / 长期 announce 失败 | 连续 N 次 tracker 状态失败，或最近一次为 `unregistered torrent` | 下载器 trackers 接口 | ❌ **未采集，需扩展** |
| `FILE_MISSING` 本地文件缺失 | 种子内容路径不存在或不可读 | `content_path` / `save_path` + `stat()` | 本地文件系统 | ✅ 可用 |

### 3.2 关键细节：不能把「站点不可达」当成「站侧删种」

站点会因风控、限流、Cookie 过期返回 404 或 403，如果直接判 `SITE_MISSING`，一次凭证过期就会生成上百条假异常，功能直接失去信任。

必须实现三道闸门：

1. **单种子连续确认**：同一 `(watch, kind)` 连续 `N` 次（默认 2）巡查命中才创建 issue。
2. **站点级熔断**：同一站点单次巡查中，若「未找到」比例 > 50% 且样本 ≥ 10，判定为**站点级异常**，本次该站点全部结果标记 `UNKNOWN`，不生成任何 issue，同时产生一条「站点凭证可能过期」的告警。
3. **错误分类**：只有「明确的 404 / 种子不存在」算 `MISSING`；超时、5xx、403、429、解析失败一律算 `UNKNOWN`，`UNKNOWN` 不参与判定，也不清除已有 issue。

### 3.3 种子身份比对（第二难点）

下载器的 `torrent_hash` 与站点的 `torrent_id` **不是同一个东西**，站点做种列表通常也不返回 infohash。必须建立稳定的身份映射，优先级如下：

| 优先级 | 来源 | 可靠性 | 说明 |
| --- | --- | --- | --- |
| P1 | `.torrent` 的 `comment` 字段中的站点详情页 URL | 高 | 项目已有实现（`build_template_from_transmission.py`），直接复用解析器 |
| P2 | PackBreaker 自己的执行记录（`unpack_execution_plan` 的 `target_downloader_id` + `metainfo_digest`，`unpack_torrent_metadata(site_key, remote_torrent_id)`） | 高 | 只覆盖本系统创建的种子 |
| P3 | 种子名称 + 精确大小 双匹配 | 中 | 仅用于「提示」，不参与自动修复，且必须唯一命中 |
| P4 | 人工绑定 | 高 | 详情抽屉提供「绑定到站点种子」入口，结果写入映射表 |

**映射只解析一次并持久化**，不要每次巡查都重新解析 comment。解析失败（P1–P3 都不可用）的种子进入「未关联」状态，UI 上明确显示「无法判定站点侧状态」，而不是默认正常。

### 3.4 站侧种子列表：真实成本核算（更正初版判断）

初版把这件事写成「每站分别适配，成本高」。核对代码后需要更正——**这个判断偏悲观**，理由如下。

**（1）18 个站里只有 2 个是独立实现**

`infrastructure/adapters/` 下只有 `nexusphp.py`、`rousi_pro.py` 两个站点实现文件，其余全部经 `NexusPhpWebAdapter` 的参数化 Profile 承载：

| 实现 | 站点 | 数量 |
| --- | --- | --- |
| `NexusPhpWebAdapter`（含 `HDTimeAdapter` / `HHClubAdapter` 子类） | HDTime、HHClub、KeepFrds、HDHome、UBits、HDFans、BTSchool、PTTime、聆音、PTerClub、Audiences、SpringSunday、HDDolby、U2、不可躺、CarPT | 16 |
| M-Team API | M-Team | 1 |
| Rousi Pro API | Rousi Pro | 1 |

因此 `list_seeding()` 加在 NexusPHP 基类上，是**一次开发覆盖 16 个站**，站点差异通过 `NexusPhpProfile` 声明（`seeding_path`、列偏移），不是 16 次开发。

**（2）这条请求路径已经存在并被生产验证过**

`nexusphp.py` 的 `fetch_user_profile` 里已经在请求做种列表：

```python
seeding_path = "/getusertorrentlist.php" if site_key == "pttime" else "/getusertorrentlistajax.php"
seeding_html = await get_profile_html(seeding_path, params={"userid": uid or "", "type": "seeding"}, ...)
```

这段代码已经处理了：站点分支、`Referer` 差异、请求间隔限流、登录页检测（`_reject_login_page`）、异常降级（辅助接口失败不触发整站重试）。

**缺的不是请求能力，是「只取汇总、丢掉了明细」**。

**（3）表格行已经被解析出来了**

`_NexusHtmlParser` 已经把 HTML 表格解析成 `rows`（`_Row` 列表），`_parse_nexus_ajax_seeding_summary` 只取了「条数 + 总大小」两个字段，行内数据被丢弃。

所以新增能力实际只需：一个 `_parse_nexus_seeding_rows()`（从现成的 `rows` 提 `torrent_id` / 名称 / 大小 / 做种时长）+ 一处分页处理 + 协议方法 `list_seeding()`。

**（4）当前触发条件是可选的，需要改为强制**

现有逻辑只在 `profile.seeding_count is None or seeding_size_bytes is None` 时才请求——多数站点从 `userdetails.php` 已能解析出汇总，所以这条请求平时**不一定发生**。巡查场景需要无条件触发，这是一处必须注意的改动点。

**（5）需要单独验证的特殊站点**

| 站点 | 特殊点 |
| --- | --- |
| PTTime | 用 `.php` 而非 `ajax.php`，解析器为 `_parse_pttime_seeding_summary` |
| U2 | `_parse_u2_ajax_seeding_summary`，结构不同 |
| Audiences | 自定义列偏移 + 单独 `Referer` |
| SpringSunday | 结构特殊，有专门的「空结果不能当 0」保护 |
| HDTime / KeepFrds | 被排除在通用分支外，各自有独立处理 |
| M-Team / Rousi Pro | API 站点，走 OpenAPI / 自有接口，另算 |

**分页行为需实测**：`getusertorrentlistajax.php` 在部分站返回全部、部分站返回单页（常见 50 条）。这是 M0 必须先验证的第一件事，决定要不要写分页循环。

### 3.5 不做站点种子列表的实际影响

| 影响 | 严重度 | 具体后果 |
| --- | --- | --- |
| 无法产出 `SITE_ONLY`（站侧有、下载器没有） | 中 | 少一类检测。但这类**误报率天然高**：PackBreaker 只能看到自己配置的下载器，用户若在第二台机器 / 另一个客户端做种，会被判成「幽灵做种」。做之前必须先做「多下载器白名单 / 忽略规则」 |
| `SITE_MISSING` 只能逐种子探测详情页 | 中高 | 1000 种子 / 24h TTL ≈ 每轮数百次请求，风控风险明显高于「1 次列表请求」。**反直觉结论：不做列表反而更贵** |
| H&R 时长只能用本地 `seeding_time` 估算 | 中 | 多数情况下本地计时 ≈ 站点计时（同任务）。差异出现在重建任务、换客户端、站点侧暂停计时三种场景，会导致 H&R 风险低估 |
| 看不到站点侧真实做种上传量 | 低 | 已有 profile 汇总字段兜底 |
| 无法发现「站点虚高计数」 | 低 | 站点多算你做种，对用户无实际损失 |

**结论**：`list_seeding` 不是「要不要做」的问题，而是「排在哪一期」的问题。它对 4 类异常里的 1 类（`SITE_ONLY`）必要，同时也是 `SITE_MISSING` 的**成本更低的实现方式**。

### 3.6 排期建议（两阶段，解耦风险）

| 阶段 | 内容 | 覆盖站点 | 产出异常类型 |
| --- | --- | --- | --- |
| **v1（本次）** | 不做 `list_seeding`。`SITE_MISSING` 走逐种子详情页探测（TTL 24h + 采样上限）；`SITE_ONLY` 类型不显示 | 全部 18 站 | `SITE_MISSING`、`TRACKER_ERROR`、`FILE_MISSING` |
| **v1.1（紧随其后）** | 在 `NexusPhpWebAdapter` 加 `list_seeding()`，先做 1 个站验证分页与行解析，再一次覆盖 16 站 | NexusPHP 系 16 站 | 新增 `SITE_ONLY`；`SITE_MISSING` 从「N 次请求」降为「1 次请求」 |
| **v1.2** | M-Team / Rousi Pro API 版 `list_seeding` | 2 站 | 全站覆盖 |

v1 的 `SITE_ONLY` tab 不显示（不是置灰），避免用户误以为「没有幽灵做种」。等 v1.1 上线再出现，UI 不需要改结构，只是 tab 由数据驱动。

---

## 4. 实时 vs 定时（明确回答）

| 问题 | 结论 |
| --- | --- |
| 巡查频率 | 定时。默认 **6 小时**，配置范围 1–24 小时，可关闭 |
| 首次巡查 | 启用后延迟 60 秒执行，避免启动风暴 |
| 页面数据 | **读数据库快照，不实时**。页面顶部显示「最近巡查 2026-11-20 09:30 / 下次巡查 15:30」 |
| 是否 WebSocket 推送 | 不做。列表页 30 秒轮询一次 run 状态仅在「巡查进行中」时开启，其余时间不轮询 |
| 「立即巡查」 | 手动触发一次同步巡查，返回 `run_id`，前端轮询进度；与定时任务共用同一个 `tick_lock`，重复触发直接返回 409 |
| 单次巡查超时 | 默认 20 分钟，超时标记 `PARTIAL`（已完成的站点结果保留，未完成的标记 `UNKNOWN`） |
| 数据新鲜度提示 | 超过 `2 × 间隔` 未成功巡查，页面顶部显示黄色提示条「巡查已停止 X 小时，数据可能过期」 |
| 与每日 03:37 站点统计的关系 | **完全独立**。站点统计只更新 ratio/bonus 快照，不产出异常；两者共用一个站点并发限制器 |

---

## 5. 数据模型（迁移 `0057_seeding_health_monitor`）

### 5.1 `seeding_watch`（巡查对象）

```sql
CREATE TABLE seeding_watch (
    id                  VARCHAR(36) PRIMARY KEY,
    downloader_id       VARCHAR(36) NOT NULL,
    torrent_hash        VARCHAR(64) NOT NULL,
    torrent_name        VARCHAR(1024) NOT NULL,
    size_bytes          BIGINT NOT NULL,
    save_path           VARCHAR(2048) NOT NULL,
    content_path        VARCHAR(2048),
    site_key            VARCHAR(64),           -- P1/P2 解析结果，可空
    remote_torrent_id   VARCHAR(64),           -- 站点侧种子 ID，可空
    identity_source     VARCHAR(16),           -- COMMENT / PLAN / NAME_SIZE / MANUAL
    identity_locked     BOOLEAN NOT NULL DEFAULT 0,  -- 人工绑定后不再自动覆盖
    seeding_time_seconds BIGINT,               -- 由巡查刷新
    tracker_state       VARCHAR(32),           -- OK / ERROR / UNKNOWN
    presence            VARCHAR(16) NOT NULL,  -- ACTIVE / GONE
    first_seen_at       TIMESTAMP NOT NULL,
    last_seen_at        TIMESTAMP NOT NULL,
    created_at          TIMESTAMP NOT NULL,
    updated_at          TIMESTAMP NOT NULL,
    CONSTRAINT uq_seeding_watch_hash UNIQUE (downloader_id, torrent_hash)
);
CREATE INDEX ix_seeding_watch_site ON seeding_watch (site_key, remote_torrent_id);
CREATE INDEX ix_seeding_watch_presence ON seeding_watch (presence, last_seen_at);
```

### 5.2 `seeding_health_issue`（异常实例）

```sql
CREATE TABLE seeding_health_issue (
    id                VARCHAR(36) PRIMARY KEY,
    watch_id          VARCHAR(36) NOT NULL REFERENCES seeding_watch(id) ON DELETE CASCADE,
    kind              VARCHAR(24) NOT NULL,   -- SITE_MISSING / SITE_ONLY / TRACKER_ERROR / FILE_MISSING
    severity          VARCHAR(8)  NOT NULL,   -- HIGH / MEDIUM / LOW
    risk_score        INTEGER NOT NULL,
    status            VARCHAR(16) NOT NULL,   -- OPEN / ACKED / REPAIRING / RESOLVED / IGNORED
    evidence          TEXT NOT NULL,          -- JSON，结构化证据快照
    suggestion        VARCHAR(32) NOT NULL,   -- RETRIEVE_TORRENT / RECHECK / RESEED
    detect_count      INTEGER NOT NULL DEFAULT 1,
    first_detected_at TIMESTAMP NOT NULL,
    last_detected_at  TIMESTAMP NOT NULL,
    resolved_at       TIMESTAMP,
    resolved_reason   VARCHAR(32),            -- AUTO / MANUAL / REPAIRED / GONE
    created_at        TIMESTAMP NOT NULL,
    updated_at        TIMESTAMP NOT NULL
);
-- SQLite 部分索引：同一对象同类异常只允许一个活跃实例
CREATE UNIQUE INDEX uq_issue_active
    ON seeding_health_issue (watch_id, kind)
    WHERE status IN ('OPEN', 'ACKED', 'REPAIRING');
CREATE INDEX ix_issue_list ON seeding_health_issue (status, kind, severity, last_detected_at);
```

### 5.3 `seeding_health_run`（巡查批次）

```sql
CREATE TABLE seeding_health_run (
    id               VARCHAR(36) PRIMARY KEY,
    trigger          VARCHAR(12) NOT NULL,   -- SCHEDULED / MANUAL
    status           VARCHAR(12) NOT NULL,   -- RUNNING / OK / PARTIAL / FAILED / CANCELLED
    started_at       TIMESTAMP NOT NULL,
    finished_at      TIMESTAMP,
    scanned_watches  INTEGER NOT NULL DEFAULT 0,
    created_issues   INTEGER NOT NULL DEFAULT 0,
    resolved_issues  INTEGER NOT NULL DEFAULT 0,
    degraded_sites   TEXT,                   -- JSON，本次熔断的站点
    error_summary    TEXT
);
```

### 5.4 `seeding_policy`（站点 H&R 规则，可空）

```sql
CREATE TABLE seeding_policy (
    site_key            VARCHAR(64) PRIMARY KEY,
    min_seeding_hours   INTEGER,       -- 如 72；NULL 表示未知
    requires_hr         BOOLEAN NOT NULL DEFAULT 1,
    source              VARCHAR(12) NOT NULL,  -- DEFAULT / MANUAL
    updated_at          TIMESTAMP NOT NULL
);
```

内置一张默认表（M-Team / HDTime / HHClub / KeepFrds / PTerClub 等），标注 `source=DEFAULT` 且 UI 明确提示「默认假设，请按站点实际规则修正」。**不要静默假装准确**。

---

## 6. 状态机

### 6.1 issue 生命周期

```
                     ┌──────────── 再次命中（复发） ────────────┐
                     │                                          │
[NEW] ──命中──> OPEN ──人工确认──> ACKED ──执行修复──> REPAIRING ──成功──> RESOLVED
                 │                    │                     │
                 │                    └──忽略──────────────> IGNORED
                 │                                          │
                 └──下次巡查不再命中（自动恢复）──────────────> RESOLVED(auto)
```

判定规则：

- **自动恢复**：本次巡查该种子不再命中该类异常 → `RESOLVED`，`resolved_reason=AUTO`。这就是「已恢复」指标的来源。
- **复发**：`RESOLVED` 后 7 天内再次命中 → 复用原记录（`status=OPEN`，`detect_count+1`），不新建，便于统计「反复出问题的种子」。
- **GONE**：`watch.presence=GONE`（下载器里种子已删除）→ 所有活跃 issue 置 `RESOLVED(GONE)`，不再巡查。
- **IGNORED 抑制**：`IGNORED` 后若 `kind` 不变则 30 天内不再重建；`kind` 变化（例如从 tracker 报错变成文件缺失）重新建。

### 6.2 run 状态

`RUNNING` → `OK`（全部站点成功）/ `PARTIAL`（有站点降级或超时）/ `FAILED`（0 个站点成功）。`PARTIAL` 仍然更新数据，但页面显示「部分站点未完成」。

---

## 7. 风险评分

评分用于排序，不用于自动决策。参数集中在 `backend/app/domain/seeding_health.py` 常量区。

```
base(kind)         SITE_MISSING=60  SITE_ONLY=50  TRACKER_ERROR=40  FILE_MISSING=55
+ hnr_pressure     未满足 H&R 且剩余 < 24h → +30；< 72h → +15
+ persistence      持续 > 3 天 → +10；> 7 天 → +15
+ coverage         该种子是唯一站点来源（无其他站做种）→ +10
- 上限 100
```

分档：`>= 80` HIGH / `>= 50` MEDIUM / 其余 LOW。

**H&R 风险卡片的独立口径**：只统计「未满足 H&R 最小做种时长」且「当前不在正常做种状态」的种子。若站点 `min_seeding_hours` 未知，卡片显示 `—` 并附「规则未知」气泡，不要用 0 冒充。

---

## 8. 后端接口契约

统一前缀 `/monitor/seeding-health`，全部要求认证；读用新权限 `MONITOR_READ`，写用 `MONITOR_WRITE`。错误沿用项目现有 `ApplicationError(code, status, title, detail)` 结构。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/summary` | 四卡片 + 类型 tab 计数 + 最近/下次巡查时间 + 数据新鲜度 |
| GET | `/issues` | 分页列表，参数 `kind/status/severity/site_id/downloader_id/q/unhandled_only/page/page_size` |
| GET | `/issues/{id}` | 详情：watch 信息 + evidence 四条 + 建议动作 + 可执行动作列表 |
| POST | `/issues/{id}/actions` | `ACK` / `IGNORE` / `RETRIEVE_TORRENT` / `RECHECK` / `RESEED` / `BIND_SITE_TORRENT` |
| POST | `/runs` | 立即巡查，返回 `202 {run_id}`；已有 run 在进行中返回 `409 SEEDING_HEALTH_RUN_ACTIVE` |
| GET | `/runs/{id}` | 进度（scanned/total/percent） |
| GET | `/runs` | 历史批次，最近 30 条 |
| GET | `/export` | CSV，受 `page_size` 上限保护，最多 5000 行 |
| GET/PUT | `/policy` | 巡查间隔、确认次数、H&R 默认值、通知开关 |

### 8.1 summary 响应示例

```json
{
  "watched": 1286,
  "healthy": 1158,
  "abnormal": 128,
  "hnr_risk": 6,
  "pending": 106,
  "recovered": 22,
  "by_kind": {"SITE_MISSING": 18, "SITE_ONLY": 9, "TRACKER_ERROR": 54, "FILE_MISSING": 47},
  "last_run": {"id": "...", "finished_at": "2026-11-20T09:30:00+08:00", "status": "OK"},
  "next_run_at": "2026-11-20T15:30:00+08:00",
  "stale": false,
  "degraded_sites": []
}
```

### 8.2 evidence 结构（对应草图 B 的四条证据）

```json
{
  "site":    {"result": "404", "consecutive_misses": 3, "detail": "站点详情页返回 404，连续 3 次巡查无记录"},
  "downloader": {"state": "seeding", "progress": 1.0, "detail": "任务存在，做种中，但 tracker 未更新"},
  "tracker": {"last_announce_at": "2026-11-19T22:14:00+08:00", "message": "unregistered torrent", "consecutive_failures": 3},
  "filesystem": {"path": "/downloads/...", "exists": true, "readable": true}
}
```

每条证据都必须带**原始值**，不能只给结论文案——用户需要判断题。前端按 `result` 渲染三种颜色（正常/异常/未知）。

---

## 9. 巡查执行流程

```
1. 前置检查：是否有 run 在跑（tick_lock）→ 有则跳过
2. 读取启用的下载器 → 并发（上限 2）调用 list_torrents()
3. 过滤：progress >= 1.0 且状态属于做种类（qB: uploading/stalledUP/queuedUP/pausedUP；TR: 6/停种）
4. upsert seeding_watch：新种子解析身份；消失的种子 presence=GONE
5. 身份解析（仅新种子或 identity 为空）：
   a. 读 .torrent comment → 详情页 URL → (site_key, torrent_id)
   b. 查 unpack 执行记录
   c. 名称+大小唯一匹配
   d. 都失败 → identity_source=NULL，本次跳过站点侧判定
6. 文件检查：content_path（无则 save_path）stat；路径不在容器可见范围 → UNKNOWN，不判缺失
7. 站点侧判定（按 site_key 分组，组内并发 4，组间串行）：
   - 有 list_seeding → 全量拉取（分页，最多 20 页）→ 双向 diff
   - 无 list_seeding → 对「未确认且 TTL 过期」的种子逐个 fetch_details
   - 统计本组 MISSING 比例 → 触发熔断则整组降级
8. Tracker 判定：
   - TR：随 list_torrents 一次取回（扩展 fields，零额外请求）
   - qB：只对「已命中其他异常」或「距上次检查 > 24h」的种子批量取 trackers，每批 50，批间 sleep 200ms
9. 落库：新建/更新 issue，计算风险分，自动 resolve 不再命中的
10. 写 run 结果 + 通知
```

### 9.1 性能预算

| 场景 | 目标 | 说明 |
| --- | --- | --- |
| 1000 种子 / 3 站点 / 全支持 list_seeding | < 60 秒 | 站点请求数 ≈ 站点数 × 分页数 |
| 1000 种子 / 站点不支持 list_seeding | < 8 分钟 | 受 24h TTL 限制，单次实际探测 ≈ 未确认种子数 |
| qB trackers 采样 | 每批 50，≤ 200ms 间隔 | 避免 qB WebUI 被拖慢 |

**必须做**：巡查期间不得阻塞 API 线程（全部 `asyncio.to_thread` 包同步 DB/文件操作，沿用 `SiteStatisticsDriver` 做法）。

---

## 10. 修复动作设计

三个动作对应草图 B 的三个按钮，安全等级不同，必须区别对待。

| 动作 | 实现 | 安全约束 | 适用异常 |
| --- | --- | --- | --- |
| `RETRIEVE_TORRENT` 重新拉取 .torrent | `site.fetch_torrent(remote_torrent_id)` → 暂存 → 以 `paused=True` 加到下载器原 `save_path` | 不删除任何本地文件；若同 hash 已存在则拒绝并提示「先删除旧任务」 | SITE_MISSING |

> **v1 实现状态**：仅 `RECHECK` 已开放；`RETRIEVE_TORRENT` 与 `RESEED` 返回 409 并提示人工处理，
> 待内容验证链与操作 journal 接线完成后再开放。
| `RECHECK` 强制重校验 | `recheck_torrent(hash)`（已有能力） | 只读操作，无风险 | TRACKER_ERROR / FILE_MISSING |
| `RESEED` 重建辅种 | 走现有内容验证链（`unpack_existing_reuse` + piece 校验）后再添加 | **必须先验证再添加**，验证不通过直接拒绝并引导去「数据拆包」 | SITE_ONLY |

统一约束：

- 所有动作写入现有 operation journal，可追溯、可对账。
- 前端点击后按钮立即置灰 90 秒，防重复提交。
- 动作失败要回填到 issue 的 `evidence`，并在 UI 显示真实错误码，不要吞异常。
- **不做全自动修复**（默认关闭）。即使后续开放，也只允许 `RECHECK`。

---

## 11. 前端实现

### 11.1 导航改动（`frontend/src/navigation.ts`）

**约束：维持现有菜单结构，现有四组及其全部子项零改动。** 只插入「监控中心」一个一级项 + 「做种健康巡查」一个二级项。不拆分、不改名、不合并任何已有项；`通知收件箱` 继续留在顶栏铃铛，不升为菜单。

最终顺序：总览 / 任务中心 / **监控中心** / 配置中心 / 系统管理。

需同步修改 6 处，漏一处会导致路由错乱：

```ts
export const PRIMARY_ROUTE_NAMES = ['总览', '任务中心', '监控中心', '配置中心', '系统管理'] as const;

export const SECONDARY_ROUTE_NAMES = [
  '数据拆包', '数据去重',
  '做种健康巡查',            // 新增
  'CookieCloud配置', '站点配置', '下载器配置',
  '通知配置', '日志浏览', 'Agent配置', '备份恢复',
] as const;

SECONDARY_ROUTE_PATHS: 做种健康巡查: '监控中心/做种健康巡查'
DEFAULT_ROUTE_BY_PRIMARY: 监控中心: '监控中心/做种健康巡查'
NAVIGATION_GROUPS: { name: '监控中心', children: ['做种健康巡查'] }
ROUTE_PARENT: '监控中心/做种健康巡查' → '监控中心'
```

同时补 `LEGACY_ROUTE_ALIASES`：`做种巡查`、`监控` → `监控中心/做种健康巡查`，保证草图 B 里的旧 hash 不 404。
`navigation.test.ts` 需补：新一级路由可归一化、面包屑为两级、默认子路由正确。

### 11.2 新增文件

| 文件 | 职责 |
| --- | --- |
| `components/SeedingHealthPanel.vue` | 页面主体：四卡片 + 类型 tab + 筛选栏 + 表格 + 分页 |
| `components/SeedingHealthDetailDrawer.vue` | 详情抽屉：证据时间线 + 修复建议 + 三动作 |
| `components/SeedingHealthSummaryCards.vue` | 四张指标卡（可复用于后续监控页） |
| `monitoringPresentation.ts` | 异常类型/风险/状态 → 文案与语义色映射，纯函数，便于单测 |
| `api/monitoring.ts` | 接口封装，全部走现有 client |
| `App.vue` | 新增一条 `v-else-if` 分支挂载 `SeedingHealthPanel` |

### 11.3 交互细节（容易被忽略，但直接影响观感）

1. **类型 tab 的计数与筛选联动**：点击 tab 后表格只剩该类型，但卡片数字不变（卡片是全量口径）。
2. **「仅看未处理」默认开启**：对应草图 A 的 toggle。`status in (OPEN, ACKED)`。
3. **风险列用文字而非纯色点**：草图里的「H&R 高风险」是标签，颜色只做辅助，避免色弱不可辨。
4. **证据列必须可截断**：单行 `text-overflow: ellipsis` + `title` 完整内容，否则表格会被长文案撑破。
5. **表格操作列**：保持项目刚统一的居中样式（`style(ui): center table row actions`），不要引入投影。
6. **空态分三种**：从未巡查过 / 巡查中 / 巡查完成但无异常。三种文案不能混用。
7. **降级提示**：`degraded_sites` 非空时，页面顶部黄条列出站点名 + 「本次未判定」，点击可跳站点配置。

### 11.4 指标卡口径（前端不可自行计算）

四张卡全部来自 `GET /summary`，前端不做二次聚合。这样口径唯一，避免前后端算错。

---

## 12. 通知与告警

| 场景 | 渠道 | 频率控制 |
| --- | --- | --- |
| 新建 HIGH 风险 issue | 站内通知 + 可选 Telegram | 同一 `(site, kind)` 每小时最多 1 条 |
| 站点级熔断（凭证可能过期） | 站内 + 复用站点健康告警 | 每站点每天最多 1 条 |
| 每日汇总 | 站内 | 每天 09:00 一条，含新增/恢复/H&R 风险数 |
| 巡查连续失败 | 站内 | 连续 3 次失败告警一次，恢复后补一条 |

`AdminNotificationService` 需新增通用写入方法，不能只靠 `record_version_update`：

```python
def record(self, *, level: str, title: str, body: str, dedup_key: str | None = None) -> None
```

`dedup_key` 用于上面的频率控制（写入前查最近同 key 记录时间）。

---

## 13. 配置项

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `PACKBREAKER_SEEDING_HEALTH_ENABLED` | `true` | 总开关 |
| `PACKBREAKER_SEEDING_HEALTH_INTERVAL_HOURS` | `6` | 巡查间隔，1–24 |
| `PACKBREAKER_SEEDING_HEALTH_STARTUP_DELAY_SECONDS` | `60` | 首次延迟 |
| `PACKBREAKER_SEEDING_HEALTH_RUN_TIMEOUT_SECONDS` | `1200` | 单次超时 |
| `PACKBREAKER_SEEDING_HEALTH_CONFIRM_THRESHOLD` | `2` | 连续确认次数 |
| `PACKBREAKER_SEEDING_HEALTH_DETAIL_TTL_HOURS` | `24` | 单种子站点探测缓存 |
| `PACKBREAKER_SEEDING_HEALTH_DEGRADE_RATIO` | `0.5` | 站点熔断比例 |
| `PACKBREAKER_SEEDING_HEALTH_TRACKER_BATCH` | `50` | qB tracker 批量 |

页面上的「立即巡查」不修改这些值；间隔等可在「配置」抽屉里按实例修改（写入 `seeding_policy` 之外的一个 settings 行）。

---

## 14. 实施计划

| 里程碑 | 内容 | 产出 | 预估 |
| --- | --- | --- | --- |
| **M0 前置（v1 必需）** | ① `DownloaderTorrent` 增 `seeding_time_seconds` / `added_at` / `tracker_state` ② TR 扩 fields（`secondsSeeding` / `doneDate` / `trackerStats`）③ qB 增 trackers 批量拉取 | 适配层改造 PR | 3 天 |
| **M1 后端核心** | 迁移 0057、`seeding_health.py` 域逻辑（判定/评分/状态机）、`SeedingHealthDriver`、`SeedingHealthService`、API + OpenAPI、单测 | 可 curl 跑通全链路 | 6 天 |
| **M2 前端** | 导航 6 处 + 3 个组件 + api 封装 + 空态/降级态 + 抽屉 | 页面可用 | 5 天 |
| **M3 打磨** | 修复动作接 journal、通知去重、CSV 导出、历史批次、`check.py`/`test.py` 全绿 | 可发版 | 3 天 |
| **M4（v1.1）** | `NexusPhpWebAdapter.list_seeding()`：先 M-Team 之外的 1 个 NexusPHP 站验证分页与行解析，再一次覆盖 16 站 | 打开 `SITE_ONLY`，`SITE_MISSING` 降为 1 次请求 | 4 天 |
| **M5（v1.2）** | M-Team / Rousi Pro API 版 `list_seeding` | 18 站全覆盖 | 3 天 |

v1 交付线为 M0–M3，不含 `list_seeding`；M4/M5 紧随其后，不阻塞 v1 发版。`SITE_ONLY` 在 v1 的 UI 中不渲染对应 tab，M4 上线后由数据自动出现，前端无需改结构。

---

## 15. 测试计划

| 层级 | 用例 |
| --- | --- |
| 域单测 | 四类判定真值表；风险评分边界（79/80）；状态机迁移（含复发、IGNORED 抑制）；熔断比例计算 |
| 驱动单测 | tick_lock 重入返回 None；超时标记 PARTIAL；站点失败不阻断其他站点 |
| 适配器契约 | qB/TR `list_torrents` 新字段解析（含缺失字段降级）；qB trackers 批量与限速；`list_seeding` 分页与去重 |
| API 契约 | summary 口径与 DB 一致性；分页边界；409 并发巡查；权限（无 MONITOR_WRITE 时 403） |
| 前端 | `monitoringPresentation` 纯函数；导航归一化与面包屑；tab 与筛选联动；空态三分支 |
| 集成 | 伪站点返回 404 洪水 → 触发熔断，不产生 issue；恢复后 issue 自动 RESOLVED |

必须覆盖的**反例**：站点 403、429、超时、HTML 解析失败、下载器离线、`content_path` 在容器外。这 6 种都不允许生成 issue。

---

## 16. 风险登记

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 站点做种列表适配工作量大（18 个站） | 功能覆盖率低 | 先做 3 个主力站，其余降级为逐种子探测；UI 明确标注支持状态 |
| qB trackers 请求放大 | 拖慢 qB / 触发限流 | 批量 + 采样 + 24h TTL，见 §9.1 |
| 站点 404 洪水误报 | 信任崩塌 | 三道闸门（§3.2） |
| 千种子下 DB 写入压力 | SQLite 锁竞争 | 批量 upsert（每 100 条一事务），WAL 已开启 |
| 身份解析失败率高 | 大量「无法判定」 | P1 优先（comment 解析），并提供人工绑定入口；页面把「未关联」单独计一档 |
| H&R 规则不准 | 误导用户 | 明确区分「站点规则 / 默认假设」，未知显示 `—` |

---

## 17. 需要你拍板的问题

1. **v1 是否接受「不含 SITE_ONLY」**：v1 只做 `SITE_MISSING` / `TRACKER_ERROR` / `FILE_MISSING` 三类，站点种子列表能力放到 v1.1 一次性覆盖 16 个 NexusPHP 站。我建议接受——逐站适配的工作量被高估了（见 §3.4），但不必和 v1 绑在一起上线。另需确认：v1.1 触发该请求时要独立限流，避免与每日 03:37 站点统计叠加。
2. **qB tracker 采样策略**：全量（准但慢）还是只查疑似异常（快但漏报）？我建议后者 + 24h 轮转补齐。
3. **H&R 规则来源**：只做手工配置，还是要尝试解析各站规则页？我建议 v1 只做手工 + 内置默认表。
4. **巡查间隔**：6 小时是否合适？如果用户更在意 H&R，可能有人想 1 小时。
5. **是否允许自动修复**：我建议 v1 全部人工确认，只提供「建议动作」。
6. **异常历史保留期**：90 天是否够？超过后是否需要归档表。
7. **「监控中心」是否预留其他子菜单位**：后续要放 H&R 看板、资产趋势，菜单结构是否现在就把位置留好（我建议留空位，先只挂一个子项）。
8. **菜单插入位置**：本方案把「监控中心」放在「任务中心」之后、「配置中心」之前（与草图 A 一致）。若你希望放最后，只改 `PRIMARY_ROUTE_NAMES` 顺序，其余不受影响。

---

## 18. 不在本次范围

- H&R 风险看板（独立页）、站点资产趋势、优惠窗口管理
- 全自动修复策略引擎
- 站点反爬策略绕过
- 多用户权限细分（沿用现有单管理员，仅新增 MONITOR_* 权限常量以便后续扩展）
