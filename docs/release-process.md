# 发布流程与供应链证据

## 1. 发布身份

PackBreaker 当前项目版本来自 `pyproject.toml`。正式发布 tag 必须精确等于 `v<project.version>`；`scripts/release_manifest.py --validate-tag-only` 会在构建前失败关闭任何版本不一致的 tag。

部署身份不是 `stable`、版本 tag 或 commit tag，而是发布清单中的：

`immutable_image = <ghcr-image>@sha256:<image-digest>`

`stable` 仅用于发现当前稳定通道，版本 tag 用于人类导航；生产安装、升级和回滚记录必须保存完整 digest。发布流水线会拒绝已存在的同版本 GitHub Release 或 GHCR 版本镜像，避免重跑覆盖不可变版本身份。

## 2. 基础镜像与目标平台

Dockerfile 的三个 `FROM` 都使用“精确补丁版本 + sha256 digest”固定基础镜像，避免构建时被同名可变 tag 悄然替换。当前发布目标只声明 `linux/amd64`，release workflow 也显式使用该平台构建。

更新基础镜像时必须显式修改 Dockerfile 中的 tag 和 digest，并重新通过容器、迁移、恢复与仓库安全门禁；不能只修改 tag 而省略 digest。

## 3. Tag 发布流水线

推送 `v*.*.*` tag 后，`.github/workflows/release.yml` 按以下顺序执行：

1. 安装冻结的 Python/Node 依赖，验证 tag 与项目版本一致。
2. 运行 `scripts/check.py` 和 `scripts/test.py` 全量质量门禁。
3. 使用 Buildx 构建并推送单平台 `linux/amd64` GHCR 镜像，记录 registry 返回的最终 image digest。
4. 只按 `<image>@<digest>` 扫描镜像并生成 SPDX JSON SBOM。
5. 生成 `packbreaker-<version>.release.json`，绑定 version/tag/commit/platform/image digest/SBOM 文件名与 SBOM SHA-256。
6. 为 release JSON 与 SBOM 生成 `SHA256SUMS`，同时上传 GitHub Actions artifact。
7. 创建同 tag 的 GitHub Release，并上传 SBOM、release manifest 与 checksums，使用自动生成的 release notes；若同版本 Release 已存在则在构建前失败关闭。
8. 只有 Release 资产全部发布成功后，才把 `stable` 通道移动到本次已经记录的不可变 image digest；中途失败不会推进稳定通道。

Buildx 同时开启 provenance 元数据，但当前 M6 不把它表述为独立签名或 artifact attestation；若后续启用签名/attestation，必须另行定义密钥、身份与验证策略。

## 4. 发布产物

每个正式版本至少包含：

- GHCR `linux/amd64` 镜像及不可变 digest；
- `packbreaker-<version>.spdx.json`；
- `packbreaker-<version>.release.json`；
- `SHA256SUMS`；
- GitHub Release notes；
- 与该版本对应的部署、升级兼容和已知限制文档。

release manifest 不保存凭证、数据库或真实环境路径。SBOM 从已经按 digest 推送的镜像生成，因此其输入与发布清单中的镜像身份一致。

## 5. 当前证据缺口

代码与 workflow 已完成；GitHub Actions CI run `34851129257` 已实际跑绿 `quality`、`browser-e2e` 与 `container`，其中 container 覆盖镜像构建、空配置启动/readiness、preflight、一致性备份、停服务离线 verify/restore 与恢复后 readiness。当前唯一尚未取得的发布供应链实证来自正式 tag：真实 GHCR registry digest、SPDX SBOM、release manifest、`SHA256SUMS` 和 GitHub Release 资产必须由首次 tag workflow 实际生成并验收。
