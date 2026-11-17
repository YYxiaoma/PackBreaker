# 官方内置模板 + 模板文件导入导出 实现文档（终稿 v2）

状态：待最终确认
范围：`unpack_template` 体系（模板拆包），不涉及普通拆包流程
迁移：新增 `0055_unpack_template_origin`

> v2 变更：导出入口改到任务列表外层操作列；只导出完成项且要求执行整体终态；目录映射只取「大包」目录名（不带绝对路径与父级）；内置模板只读、不可复制；不做模板列表页；预留社区/云端模板扩展位。

---

## 0. 背景与现状

### 0.1 现有模板数据模型

| 表 | 作用 | 关键约束 |
|---|---|---|
| `unpack_template` | 模板头 | `name` 全局唯一；`current_version` 指向当前版本 |
| `unpack_template_version` | 模板版本（不可变） | `(template_id, version)` 唯一；`status ∈ {IMPORTING, READY, INCOMPLETE}`；`site_keys` 为站点类型列表 |
| `unpack_template_seed` | 版本内的种子 | `(template_version_id, metadata_id)` 唯一；`mapping_rules.source_mappings` 存「大包目录名 → 相对影片目录 → 影片文件」 |
| `unpack_torrent_metadata` | 种子元数据 | `(site_key, remote_torrent_id)` 唯一；含 `display_name / descriptor / total_size / file_summary / external_ids / metadata_digest` |
| `unpack_template_import_job` | 从站点拉取的导入作业 | 逐条请求站点详情 API |

### 0.2 现在的唯一入口

`POST /unpack/templates/import`（`site_id` + `torrent_ids` + 可选 `seed_mappings`）→ 后台逐个请求站点详情 → 写 metadata + seed → 版本置 READY。

问题：新用户没有模板时下拉为空，且没有 UI 入口。

### 0.3 三条硬约束

1. **模板必须且只能绑定一个站点类型**：`len(version.site_keys) == 1`，所选站点 `site.type` 必须等于该 `site_key`，否则拒绝跨站执行。
2. **拆包定义钉住版本**：`definition.template_version` 存具体版本号，模板升级不影响已建/在跑任务。
3. **`collection_root_name` 必须是单级安全目录名**（`_normalize_seed_mappings` 校验：无 `/`、无 `..`、非空、≤255）。导出推导必须遵守，直接决定了映射的取值口径。

---

## 1. 功能一：内置官方模板

### 1.1 目标 / 非目标

**目标**
- 镜像内置官方模板，启动后模板下拉直接可用
- 用户没配对应站点时，模板可见但置灰并提示
- 官方模板**只读**：不可删除、不可修改、不可复制，仅可在下拉框中选择

**非目标**
- 不做模板内容编辑、不做复制派生
- 不做云端拉取 / 社区模板（预留扩展位，见 §7）
- 不做模板列表管理页

### 1.2 数据存放（镜像内静态资源）

```
resources/official_templates/
├── index.json
└── stephen-chow-mnhd-mteam.pbtemplate     # 与功能二共用同一文件格式
```

`index.json`：

```json
{
  "schema_version": 1,
  "templates": [
    {
      "builtin_key": "stephen-chow-mnhd@MTEAM",
      "name": "周星驰合集 MNHD-FRDS @M-Team",
      "description": "34 部合集，模板记录影片目录与种子的对应关系",
      "site_key": "MTEAM",
      "version": 1,
      "file": "stephen-chow-mnhd-mteam.pbtemplate",
      "sha256": "<文件摘要>",
      "seed_count": 34,
      "min_app_version": "1.1.6"
    }
  ]
}
```

- 目录可通过 `PACKBREAKER_OFFICIAL_TEMPLATE_DIR` 覆盖（默认指向上面的资源目录），便于运维外挂
- 目录缺失 / 为空 / index 损坏 → **fail-soft**：只记 warning，应用照常启动

### 1.3 安装时机与幂等

启动时（lifespan，数据库就绪之后）：

```
install_builtin_templates(session_factory, official_dir)
  ├─ 读 index.json，逐条校验（schema / 必填 / sha256 / min_app_version）
  ├─ 已安装且 builtin_version >= index 版本 → 跳过
  ├─ 已安装但 index 版本更高 → 新增一个 template_version（保留旧版本）
  ├─ 未安装 → 新建 template（origin=BUILTIN, builtin_key, builtin_version）+ version 1
  └─ 单条失败只跳过该条；整体异常吞掉并记 error 日志
```

解析与落库**复用功能二的导入器**，保证内置与文件导入同一套校验与落库路径。

### 1.4 只读语义

- 新列 `origin ∈ {BUILTIN, EXPORTED, USER}`（默认 `USER`，历史数据归为 `USER`；`COMMUNITY` 由 §7 预留）
- `origin=BUILTIN`：**禁止**改名、改描述、删除模板/版本，也**不提供复制**；接口层统一返回 `UNPACK_TEMPLATE_READONLY`
- UI 只在下拉框中出现，名称前缀「官方」徽标；无任何编辑/删除/复制入口

### 1.5 与站点绑定的呈现

模板记录**站点类型**（`site_key`），不是具体站点配置 id：

- 用户有该类型且已启用的站点 → 可直接建任务
- 没有 → 下拉条目置灰，提示「需要先配置并启用 M-Team 站点」
- 同类型多个站点 → 让用户选一个（现有逻辑已支持）

---

## 2. 功能二：模板文件导出 / 导入

### 2.1 文件格式 `.pbtemplate`

ZIP（deflate），UTF-8，两个成员：

```
manifest.json     # 小，先读，用于快速校验与预览
payload.json      # 大，模板正文
```

`manifest.json`：

```json
{
  "schema_version": 1,
  "kind": "packbreaker.unpack.template",
  "name": "周星驰合集 MNHD-FRDS @M-Team",
  "description": "…",
  "site_key": "MTEAM",
  "seed_count": 34,
  "mapped_media_count": 34,
  "payload_sha256": "<payload.json 的 sha256>",
  "exported_at": "2026-11-16T07:00:00Z",
  "exporter_version": "1.1.6",
  "source": { "definition_name": "测试2" }
}
```

`payload.json`：

```json
{
  "schema_version": 1,
  "site_key": "MTEAM",
  "seeds": [
    {
      "remote_torrent_id": "123456",
      "display_name": "整蛊专家.Tricky.Brains.1991.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS",
      "descriptor": { "...": "解析出的片名/年份/季集等" },
      "total_size": 3565961212,
      "file_summary": [ { "path": "…/x.mkv", "length": 3565893814 } ],
      "external_ids": [ { "kind": "imdb", "value": "tt0103035" } ],
      "mappings": [
        {
          "collection_root_name": "周星驰.Stephen.Chow.1988-2017.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS",
          "relative_directory": "整蛊专家.Tricky.Brains.1991.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS",
          "relative_file_name": "Tricky.Brains.1991.…mkv",
          "media_title": "整蛊专家"
        }
      ]
    }
  ]
}
```

### 2.2 导出：任务列表外层操作列

**入口**：拆包任务列表的**操作列**新增「导出模板」，不在执行详情里。

按钮可用条件（全部满足，否则置灰并给 tooltip 说明原因）：

| 条件 | 说明 |
|---|---|
| 该任务有执行记录 | 没有执行记录 → 灰 |
| 执行状态为终态 | `COMPLETED` / `COMPLETED_WITH_ERRORS` / `FAILED` / `CANCELLED`，其余（匹配中、内容校验中、待审核、执行中…）→ 灰 |
| 全部影片条目均为终态 | 防守性校验：无 `MATCH_PENDING / MATCHING / TORRENT_FETCHING / CONTENT_VERIFYING / PLAN_PENDING / EXECUTING / CLIENT_VERIFYING` |
| 至少 1 条 `COMPLETED` | 为 0 时置灰（失败型任务无可导出内容） |

**导出范围**：仅 `COMPLETED` 条目。`NO_MATCH / MATCH_ERROR / CONTENT_MISMATCH / EXECUTION_ERROR` 全部跳过，并在结果提示里给出跳过数量。

**跨站点**：若完成项涉及多个站点类型，弹窗让用户选一个站点类型，只导出该类型的完成项（对应决议 1-A）。

**字段推导（全离线，不请求站点）**：

| 目标字段 | 来源 |
|---|---|
| `site_key` | 由候选 `site_id` 反查 `site.type` |
| `remote_torrent_id` | 候选 `candidate_key` |
| `display_name` | 候选 `title` |
| `total_size` | 候选 `size_bytes` |
| `descriptor` | 复用 `parse_media_name(title)` |
| `external_ids` | 候选 `imdb_id / douban_id` |
| `file_summary` | 冻结计划 `actions` → `{path: torrent_path, length}` |
| `mappings` | 见下方推导口径 |

**映射推导口径（关键，按评审意见修正）**

只映射「大包」这一级，不带绝对路径、不带 NAS 父级目录：

```
任务来源目录 = definition.source_config.directory_path
             例：/mnt/nas/downloads/待刮削/电影/周星驰.Stephen.Chow.1988-2017.…
影片来源路径 = item.source_snapshot.path
             例：…/周星驰.Stephen.Chow.1988-2017.…/整蛊专家.Tricky.Brains.1991.…/xxx.mkv

collection_root_name  = basename(来源目录)  ← 即「大包」这一级的目录名
                        → "周星驰.Stephen.Chow.1988-2017.…"
                        （不是 /mnt/nas/…，不是整条绝对路径，不带父级）
relative_directory    = 影片所在目录相对「来源目录」的路径
                        → "整蛊专家.Tricky.Brains.1991.…"
relative_file_name    = 影片文件名
media_title           = 候选标题清洗后的片名
```

补充规则：

- 若 `collection_root_name` 校验不通过（含 `/`、`..`、超长），或影片路径不在来源目录下 → 该条目**跳过并计入「无法推导映射」**，在导出结果里报告，不阻断整体导出
- 不对整个父目录链做映射；`/mnt/nas/...` 这类绝对前缀一律不进入模板文件
- 导出对话框展示推导出的「大包目录名」并可**手动覆盖**：默认值取 `basename(来源目录)`；若某条目的 `relative_directory` 出现 ≥2 级（说明来源目录可能不是大包本身，而是更上层的父目录），对话框给出提示，让用户确认或改成正确的目录名
- 跨条目的 `collection_root_name` 必须一致；不一致的条目单独列出，由用户决定是否排除

**脱敏**：不导出站点配置 id、Cookie、下载器 id、绝对路径、用户其它信息。`source.definition_name` 为可选字段。

**降级说明**：`file_summary` 由计划动作合成，缺少站点详情里的分片/文件类型等附加信息；模板匹配只依赖 `path/length`（`_mapped_torrent_file_size`、`candidate_video_file_count`），因此可用。文件中标 `"file_summary_source": "execution_plan"`。

### 2.3 导入：文件 → 模板

入口在**新建拆包任务向导的「选择模板」步骤**（见 §2.5），两个接口两步确认：

1. `POST /unpack/templates/import-file?dry_run=true`（multipart）
   → 只解析校验，返回预览：模板名/描述/站点类型/种子数/映射数/摘要是否匹配/重名处理建议/将跳过的条目
2. `POST /unpack/templates/import-file`（同文件，`dry_run=false`）
   → 落库，返回模板 id + 版本 + 结果；成功后前端自动把新模板选中

校验清单（任一失败整包拒绝）：

| 项 | 规则 |
|---|---|
| 大小 | zip ≤ 32 MiB；解压后 `payload.json` ≤ 64 MiB |
| schema | `schema_version == 1`；更高版本明确报错 |
| 摘要 | `payload_sha256` 必须匹配 |
| 条目 | `seeds` ≤ 5000；`remote_torrent_id` 去重 |
| 路径 | `collection_root_name` 单级安全名；`relative_directory` 安全相对路径（复用 `_normalize_seed_mappings`） |
| 字符串 | name ≤ 120、description ≤ 2000、path ≤ 1024 |
| 站点 | `site_key` 必须存在于站点档案表 |

落库规则：

- 模板名冲突 → 自动加后缀「（导入 2）」，预览明示
- metadata 按 `(site_key, remote_torrent_id)` upsert：不存在新建；摘要相同复用；摘要不同覆盖并记录来源
- 版本 `status`：全部成功 `READY`；部分失败 `INCOMPLETE`（详情可见失败原因）
- 用户文件导入 → `origin=EXPORTED`

### 2.4 数据模型变更（迁移 0055）

```sql
ALTER TABLE unpack_template ADD COLUMN origin TEXT NOT NULL DEFAULT 'USER';
ALTER TABLE unpack_template ADD COLUMN builtin_key TEXT;
ALTER TABLE unpack_template ADD COLUMN builtin_version INTEGER;
CREATE UNIQUE INDEX uq_unpack_template_builtin_key
    ON unpack_template(builtin_key) WHERE builtin_key IS NOT NULL;
```

- 历史行归为 `USER`，行为不变
- 不新增表，模板正文仍落在现有 4 张表

### 2.5 API 变更

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/unpack/templates/import-file` | multipart + `dry_run`，返回预览或导入结果 |
| POST | `/unpack/executions/{id}/template-export` | 返回 `.pbtemplate` 文件流（`Content-Disposition` 带安全文件名） |
| GET | `/unpack/templates` | 响应新增 `origin`、`builtin_key`、`available_site_ids`（该类型下用户已启用的站点）、`exportable` 相关的必要字段 |
| GET | `/unpack/definitions`（或任务列表接口） | 每条任务增加 `template_exportable` + `template_export_blocked_reason`，供操作列判断置灰与 tooltip |

同步更新 `frontend/openapi.json` 快照与 `schema.ts`。

### 2.6 前端

**A. 任务列表操作列**
- 新增「导出模板」按钮，与现有「执行 / 查看 / 重试」并列
- 置灰时 tooltip 说明原因（未完成 / 仍在运行 / 无完成项）
- 点击 → 若跨站点则先选站点类型 → 触发下载 `.pbtemplate`，提示导出 N 条、跳过 M 条

**B. 新建任务向导 →「选择模板」步骤**
- 模板下拉框宽度收窄
- **右侧并列**新增「导入模板」按钮（同一行）
- 点击 → 选文件 → 预览对话框（模板名/站点/种子数/映射数/重名处理）→ 确认 → 导入成功自动选中新模板

**C. 不做模板列表页**：模板只通过下拉框出现与选择

**D. 模板选择呈现**
- 内置项标「官方」徽标
- 用户无对应站点时置灰并提示「需先配置 XX 站点」
- 内置项无任何编辑/删除/复制入口

---

## 3. 实施拆分

| 阶段 | 内容 | 依赖 |
|---|---|---|
| P1 | 迁移 0055 + `origin/builtin_key` + 只读拦截（含禁止复制） | 无 |
| P2 | 文件格式 + 导入器（复用落库逻辑）+ 全量校验 + 单测 | P1 |
| P3 | 导出器（执行 → 文件，含映射推导口径）+ 下载接口 + `template_exportable` 字段 + 单测 | P2 |
| P4 | 前端：操作列导出按钮、模板步骤宽度 + 导入按钮 + 预览、官方徽标 | P2、P3 |
| P5 | 启动内置安装 + `resources/official_templates` 空目录 + 1 条样例 | P2；真实数据待提供 |
| P6 | 文档：README 增补「模板文件格式 v1」与运维说明 | 全部 |

预估：P1–P4 约 1.5–2 天；P5 待模板数据到位后 0.5 天。

---

## 4. 测试计划

**单元**
- 文件校验：schema 过高 / 摘要不符 / 超限 / 非法路径 / 重复 torrent_id
- 映射推导：`collection_root_name` 只取大包目录名（断言不含 `/`、不含 `/mnt/nas` 前缀）、路径不在来源目录下时跳过并报告、多层相对路径时给出提示
- 导出范围：只导 `COMPLETED`，其余状态全部跳过
- 可导出判定：非终态执行置灰、无完成项置灰、终态且有完成项可导出
- 导入落库：新 metadata / 同摘要复用 / 异摘要覆盖 / `INCOMPLETE` 分支
- 内置安装：幂等、版本升级新增版本、index 损坏 fail-soft、重名加后缀
- 只读：`origin=BUILTIN` 的改名/删除/复制全部被拒

**集成 / API**
- 导出→导入回环：种子数、映射数、站点类型一致；`collection_root_name` 与源一致
- 权限：写权限 + CSRF；未登录 401
- 迁移 0055 upgrade/downgrade

**回归**
- 模板拆包现有链路：导入作业、模板匹配、`unpack_template_match_acceptance`
- `check.py`（ruff / mypy / prettier / vue-tsc / OpenAPI 契约）

---

## 5. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 内置模板与用户模板重名 | 唯一约束冲突导致启动失败 | 加后缀 + 整段 fail-soft |
| 镜像体积 | 每模板几十 KB～数 MB | 只放 ZIP，index 记录体积上限，构建期校验 |
| 模板数据陈旧 | 匹配命中率下降 | 模板版本可升级；失败只影响优先级，不影响普通拆包 |
| 导出含真实目录名 | 轻微信息暴露 | 只含大包名/影片目录名，不含绝对路径；提供「不含映射」导出选项（默认包含） |
| 恶意文件 | 路径穿越 / 内存爆 | 全量校验 + 大小上限 + 白名单成员名 + 内存解析不落盘 |
| `file_summary` 精度 | 个别模板匹配降级 | 标注来源；后续可加在线补全 |

---

## 6. 评审决议（已确认）

| # | 议题 | 决议 |
|---|---|---|
| 1 | 跨站点导出 | **A**：选一个站点类型，只导出该类型完成项 |
| 2 | 导出范围 | 只导出 `COMPLETED` 条目 |
| 3 | 目录映射 | 包含；但只映射「大包」目录名（如 `周星驰.合集`），不得映射 `/mnt/nas/周星驰.合集` 这类绝对/父级路径 |
| 4 | 内置模板升级 | **A**：镜像内版本更高时自动新增版本，旧版本保留 |
| 5 | 内置模板操作 | 不可删除、不可修改、不可复制，只能在下拉框中选择 |
| 6 | 文件后缀命名 | `.pbtemplate` + `{模板名}-{site_key}-v{版本}.pbtemplate` |
| 7 | 导出入口 | 任务列表**外层操作列**，仅终态且全部条目终态、且有完成项时可用；不在执行详情 |
| 8 | 导入入口 | 新建任务向导「选择模板」步骤，下拉收窄 + 右侧并列「导入模板」 |
| 9 | 模板列表页 | 不做 |

---

## 7. 未来扩展位（本期不实现，仅预留）

后续可能增加「云端拉取」或「社区模板」模块，位置在模板拆包页面右侧。本期为其预留三点，避免回头改结构：

1. `origin` 枚举预留 `COMMUNITY`（迁移时只建 `BUILTIN/EXPORTED/USER`，枚举加值不需要再迁移）
2. 导入器抽成独立函数 `import_template_payload(payload, origin)`，云端拉取只需换成另一个 payload 来源
3. 模板下拉数据源保留 `origin` 字段，UI 可按来源分组；任务类型卡片区在「模板拆包」右侧放一张**置灰的「模板市场」占位卡片**（`disabled`、虚线边框、标注「开发中」，只做预告不可选择），说明后续可从云端与社区浏览并一键导入模板；接入时把它换成真正可点的入口即可
