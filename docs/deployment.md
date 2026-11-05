# Docker 部署指南

本文覆盖 PackBreaker 的容器化部署：Compose、`docker run`、群晖 Container Manager、反向代理、升级回滚与备份恢复。

镜像同时提供 `linux/amd64` 与 `linux/arm64`：

- GHCR：`ghcr.io/yyxiaoma/packbreaker`
- Docker Hub：`yyxiaoma01/packbreaker`
- 正式版本与不可变 digest 以 [GitHub Releases](https://github.com/YYxiaoma/PackBreaker/releases/latest) 为准

## 部署前确认

| 项目 | 要求 |
| --- | --- |
| 架构 | `linux/amd64` 或 `linux/arm64` |
| Docker | 20.10+，建议安装 Compose v2 |
| 端口 | 默认容器内 `8000`，宿主机可映射为任意端口 |
| 持久化 | 必须持久化 `/config`（数据库、配置与凭证） |
| 媒体目录 | 宿主机路径真实存在，容器内可读；需要写入的目标可写 |
| 用户 | `PUID` / `PGID` 应与媒体目录属主一致 |
| 文件系统 | Hardlink 要求源与目标在同一文件系统 |
| 时区 | 通过 `PACKBREAKER_TIMEZONE` 指定，默认 `Asia/Shanghai` |

> `/config` 中包含数据库和受保护凭证。迁移、升级或重建容器前先备份，不要直接复制运行中的 SQLite 文件。

## 方案一：Docker Compose（推荐）

### 1. 准备目录

```bash
mkdir -p /opt/packbreaker
cd /opt/packbreaker
```

确认媒体目录存在，并记录宿主机 UID / GID：

```bash
id -u
id -g
ls -ld /path/to/downloads
```

### 2. 编写 compose.yaml

```yaml
services:
  packbreaker:
    image: ghcr.io/yyxiaoma/packbreaker:stable
    container_name: packbreaker
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      PUID: "1000"
      PGID: "1000"
      PACKBREAKER_TIMEZONE: "Asia/Shanghai"
      # 反向代理场景：填写代理 IP 或 CIDR，多个用逗号分隔
      # PACKBREAKER_TRUSTED_PROXIES: "172.17.0.1/32"
    volumes:
      - packbreaker-config:/config
      - /path/to/downloads:/downloads
      # - /path/to/downloads2:/downloads2
      # 可选：Web 一键升级（授予容器 Docker 主机管理权限）
      # - /var/run/docker.sock:/var/run/docker.sock
    security_opt:
      - no-new-privileges:true

volumes:
  packbreaker-config:
```

说明：

- 媒体目录可以挂到任意容器绝对路径（如 `/downloads`），界面中使用容器路径。
- 需要多个媒体库时继续追加挂载，不要在容器内做软链接绕过路径映射。
- 只有需要 Web 一键升级时才挂载 `docker.sock`；这等价于把 Docker 主机管理权限交给容器。

### 3. 启动

```bash
docker compose pull
docker compose up -d
docker compose ps
docker logs packbreaker
```

首次启动日志中会输出管理员 `admin` 的临时密码。登录后立即修改密码。

访问：`http://服务器IP:8000`。

### 4. 部署后验收

```bash
# 容器健康状态应为 healthy
docker inspect --format '{{.State.Health.Status}}' packbreaker

# 就绪探针
curl -fsS http://127.0.0.1:8000/api/v1/health/ready
```

然后在 Web 中依次完成：

1. 添加下载器并测试连接。
2. 添加站点（Cookie / API Key）或配置 CookieCloud，执行连接测试。
3. 用一个范围很小的手动任务验证发现、候选、内容验证与执行计划。
4. 确认无误后再启用监控任务。

## 方案二：docker run（单容器）

不使用 Compose 时：

```bash
docker run -d \
  --name packbreaker \
  --restart unless-stopped \
  -p 8000:8000 \
  -e PUID=1000 \
  -e PGID=1000 \
  -e PACKBREAKER_TIMEZONE=Asia/Shanghai \
  -v packbreaker-config:/config \
  -v /path/to/downloads:/downloads \
  --security-opt no-new-privileges:true \
  ghcr.io/yyxiaoma/packbreaker:stable
```

查看日志与健康状态：

```bash
docker logs packbreaker
docker inspect --format '{{.State.Health.Status}}' packbreaker
```

## 方案三：群晖 Container Manager

适用于 DSM 7.x 的 Container Manager（原 Docker 套件）。

1. 打开 **Container Manager → 项目 → 新增**。
2. 选择 **创建 docker-compose.yml**，粘贴方案一中的配置。
3. 项目目录选择例如 `/volume1/docker/packbreaker`。
4. 把媒体目录改成群晖真实路径，例如 `/volume1/media/downloads:/downloads`。
5. `PUID` / `PGID` 使用群晖账号的实际 UID / GID（SSH 执行 `id -u <用户名>`、`id -g <用户名>`）。
6. 启动项目，等待状态变为运行中，再访问 `http://NAS地址:8000`。

注意：

- 群晖的 `docker` 目录与媒体目录通常不在同一存储卷；跨卷无法 Hardlink，只能 COPY。需要硬链接时，把源和目标放在同一卷。
- 如果 8000 端口被占用，改宿主机映射端口（例如 `"8080:8000"`），容器内端口保持 8000。
- DSM 防火墙需放行对应端口。

## 反向代理与 HTTPS

应用自带登录，但直接暴露公网仍建议放在 HTTPS 反向代理之后，并只允许可信网络访问。

### Caddy

```caddyfile
packbreaker.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

如果 Caddy 与应用在同一 Docker 网络中：

```caddyfile
packbreaker.example.com {
    reverse_proxy packbreaker:8000
}
```

同时把代理地址加入可信代理，否则应用无法正确识别客户端 IP 与协议：

```yaml
environment:
  PACKBREAKER_TRUSTED_PROXIES: "172.17.0.1/32"
```

多个代理用逗号分隔，支持 IP 或 CIDR。

### Nginx

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

反向代理场景同样需要配置 `PACKBREAKER_TRUSTED_PROXIES`。

## 目录、权限与硬链接

| 路径 | 用途 | 建议 |
| --- | --- | --- |
| `/config` | 数据库、配置、凭证、日志 | 必须持久化并备份 |
| `/downloads` | 媒体目录（示例） | 只读源 + 可写目标；按实际库位调整 |
| `/data` | 应用运行数据 | 一般无需单独挂载；如手动种子等产物需保留，可额外持久化 |

- `PUID` / `PGID` 决定容器进程身份；权限不足时优先核对目录属主，而不是使用 `chmod 777`。
- Hardlink 失败通常是源与目标跨文件系统，或挂载方式导致不在同一 device；此时只能 COPY。
- 下载器（qB / TR）必须能访问最终保存路径；跨容器部署时，确保两边挂载的是同一宿主机目录。
- 源媒体不应被下载器修改；需要恢复下载的目标必须先隔离为独立 inode。

## 升级与回滚

### Compose 升级

```bash
cd /opt/packbreaker
docker compose pull
docker compose up -d
docker compose ps
docker logs --tail 100 packbreaker
```

### 固定版本或 digest

生产环境建议固定版本标签，或使用 Releases 提供的不可变 digest：

```yaml
image: ghcr.io/yyxiaoma/packbreaker@sha256:<digest>
```

这样即使上游标签移动，重建容器仍使用同一镜像。

### 回滚

1. 把 `compose.yaml` 中的镜像改回上一版本或上一 digest。
2. `docker compose up -d`。
3. 如本次升级已执行数据库迁移，仅回退镜像不足以回退数据库；需要同时恢复升级前的 `/config` 备份。

> Web 一键升级需要挂载 `docker.sock`，且不会自动修改宿主机 `compose.yaml` 中的镜像声明。

## 备份与恢复

### 推荐：使用应用内备份

系统管理中的备份功能会按应用约束生成一致性备份，恢复时保持应用停止或遵循维护工具的离线要求。

### 卷级备份（可选）

```bash
docker stop packbreaker

docker run --rm \
  -v packbreaker-config:/config \
  -v /path/to/backup:/backup \
  alpine sh -c 'tar czf /backup/packbreaker-config-$(date +%F-%H%M).tar.gz -C /config .'

docker start packbreaker
```

恢复时先停止容器，将备份解压回 `/config`，确认属主与 `PUID` / `PGID` 一致后再启动。

## 常见问题

| 现象 | 排查方向 |
| --- | --- |
| 端口被占用 | `ss -lntp \| grep 8000`，改宿主机映射端口 |
| 容器 unhealthy | `docker logs packbreaker`；检查 `/config` 权限与磁盘空间 |
| 登录看不到临时密码 | 临时密码只在首次初始化时输出；确认日志未被清理，必要时按备份/恢复流程处理 |
| 文件找不到 / 无写权限 | 挂载路径、容器内路径、`PUID` / `PGID`、目标目录属主 |
| Hardlink 失败 | 源与目标是否同一文件系统；跨卷只能 COPY |
| 站点搜索超时 | 站点网络、凭证、代理、限流与站点 Profile 超时 |
| 反向代理后 IP / 协议不对 | 配置 `PACKBREAKER_TRUSTED_PROXIES` |
| 升级后数据库异常 | 检查迁移记录；必要时回退镜像并恢复升级前备份 |

提交问题时请提供版本、架构、错误码、复现步骤和脱敏日志；不要上传 Cookie、Passkey、API Key、真实 torrent 或完整私有媒体路径。

## 相关文档

- [项目文档索引](README.md)
- [支持矩阵](support-matrix.md)
- [已知限制](known-limitations.md)
- [测试说明](testing.md)
- [开发约束](../AGENTS.md)
