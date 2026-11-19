# PackBreaker v1.1.13

两个现场问题的修复：Transmission 4.0.x 连不上，以及 CookieCloud 换域名后只提示「测试失败」看不出原因。

## 修复：Transmission 4.0.x 连接失败

适配器此前只实现了 Transmission **4.1 起**的 JSON-RPC 2.0 信封（`jsonrpc`/`params`/`result` + snake_case），而 **4.0.x 仍是旧信封**：方法是 `session-get`，参数在 `arguments` 里，响应是 `{"result":"success","arguments":{…}}`（没有 `jsonrpc`/`id`），字段名在 session 侧是 `download-dir`、在 torrent 侧是 `hashString`/`percentDone`。

于是连接测试会在校验响应标识那一步直接失败，报「Transmission JSON-RPC 响应标识无效」。

现在：

- 首次调用自动探测协议方言，识别出旧信封就按旧格式重发同一次调用，方言缓存在连接上，不会每次多打一轮
- 旧信封的请求方法与参数做显式映射，响应递归归一成 snake_case，上层解析逻辑不变
- 4.0.x 的加种、查询、改文件选择、删种、运行时指标都在真实 4.0.5 上验证通过（含 `torrent-added`/`hashString` 回包）

## 修复：CookieCloud 测试失败看不到原因

- 草稿测试的失败原因（后端消息 + 错误码）现在常驻显示在「连接状态」卡片里，不再只有一闪而过的提示；下次测试成功自动清空
- HTTP 404 的提示补上排查方向：除了 UUID 不对，也可能是服务器地址少了路径前缀（例如原本是 `http://ip:3000/cookiecloud`，换成域名时漏了 `/cookiecloud`）

## 升级说明

- 无数据库迁移（仍为 `0056_movie_dedup_shared_link_bytes`）
- 已保存的下载器与 CookieCloud 配置不需要改动；Transmission 用户在升级后直接重新测试连接即可

## 包含 v1.1.12 的内容

- 在线升级进度条按阶段上限缓慢增长、升级期间按钮锁定、成功后自动刷新
- 「关于」页新增升级日志卡片（随镜像发布，离线可见）

## 镜像

- `ghcr.io/yyxiaoma/packbreaker:1.1.13`
- `docker.io/yyxiaoma01/packbreaker:1.1.13`
- 支持 `linux/amd64` 与 `linux/arm64`，不可变摘要、SBOM 与 SHA256SUMS 见 GitHub Release 附件。
