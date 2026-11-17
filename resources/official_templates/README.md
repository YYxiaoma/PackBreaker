# 官方内置模板目录

放进这里的模板会在 PackBreaker 启动时自动安装到数据库（幂等，可随镜像升级）。

## 用法

1. 把 `.pbtemplate` 文件放到本目录
2. 在 `index.json` 的 `templates` 数组里登记一条：

```json
{
  "builtin_key": "stephen-chow-mnhd@MTEAM",
  "name": "周星驰合集 MNHD-FRDS @M-Team",
  "description": "34 部合集",
  "site_key": "MTEAM",
  "version": 1,
  "file": "stephen-chow-mnhd-mteam.pbtemplate",
  "sha256": "<文件的 sha256>",
  "seed_count": 34,
  "min_app_version": "1.1.6"
}
```

3. 重启应用即自动安装；升级时把 `version` 加 1 并替换文件，旧版本会保留（已建任务钉在旧版本上，不受影响）

## 注意

- `builtin_key` 全局唯一，是幂等安装的主键，改动它会被视为新模板
- `sha256` 不匹配或文件缺失时只跳过该条并记 warning，不影响启动
- 内置模板在界面上只读：不可删除、修改或复制，只能在下拉框中选择
- `min_app_version` 高于当前版本时跳过安装
