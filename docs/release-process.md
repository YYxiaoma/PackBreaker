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

1. 安装冻结的 Python/Node 依赖，验证 tag 与项目版本一致，并拒绝已经存在的同 tag GitHub Release。
2. 校验 `release-baseline.json`，并通过 GitHub Releases API 强制其 tag 必须等于当前最新正式 Release；这样后续版本不能跳过直接上一正式版本的兼容门禁。
3. 运行 `scripts/check.py` 和 `scripts/test.py` 全量质量门禁。
4. 本地构建 release candidate，先执行“上一正式 release 不可变 digest → 当前候选 → 恢复升级前备份并回滚上一正式 digest”的兼容门禁，再执行独立 updater helper 的真实 Docker 成功升级与故障候选自动数据库/容器回滚 E2E；两条门禁都通过前不推送新正式镜像。
5. 使用 Buildx 构建并推送单平台 `linux/amd64` GHCR 镜像，记录 registry 返回的最终 image digest。
6. 只按 `<image>@<digest>` 扫描镜像并生成 SPDX JSON SBOM。
7. 生成 `packbreaker-<version>.release.json`，绑定 version/tag/commit/platform/image digest/SBOM 文件名与 SBOM SHA-256，并为 release JSON 与 SBOM 生成 `SHA256SUMS`。
8. 上传 GitHub Actions evidence artifact，创建同 tag 的 GitHub Release，并发布 SBOM、release manifest、checksums 与自动 release notes。
9. 只有 Release 资产全部发布成功后，才把 `stable` 与兼容 Docker 默认习惯的 `latest` 通道一起移动到本次已经记录的不可变 image digest；中途失败不会推进这两个可移动通道。

Buildx 同时开启 provenance 元数据，但当前 M6 不把它表述为独立签名或 artifact attestation；若后续启用签名/attestation，必须另行定义密钥、身份与验证策略。

若历史版本缺少 `stable`/`latest` 或可移动通道需要修复，可手工运行 `Sync Release Channels` workflow。该 workflow 只读取 `release-baseline.json`，并在确认 baseline tag 仍等于 GitHub 最新正式 Release 后，把两个通道同步到 baseline 的不可变 digest；它不会构建或重发镜像内容。

## 4. 发布产物

每个正式版本至少包含：

- GHCR `linux/amd64` 镜像及不可变 digest；
- `packbreaker-<version>.spdx.json`；
- `packbreaker-<version>.release.json`；
- `SHA256SUMS`；
- GitHub Release notes；
- 与该版本对应的部署、升级兼容和已知限制文档。

release manifest 不保存凭证、数据库或真实环境路径。SBOM 从已经按 digest 推送的镜像生成，因此其输入与发布清单中的镜像身份一致。

## 5. 当前发布证据

当前最新正式版本为 `v0.1.8`。Release workflow run `35366864779` 已成功完成 tag/version、上一正式版 baseline、全量质量门、`v0.1.7 → v0.1.8 → v0.1.7` 真实 Docker 升级/回滚、真实 updater helper 升级与自动回滚 E2E，并发布 linux/amd64 GHCR 镜像、release manifest、SPDX SBOM、`SHA256SUMS` 与 GitHub Release 资产。release 身份绑定 commit `7ee1625786ff58c128f6b0948100b97b9153e5cd` 和不可变 digest `sha256:f114296a40c3bc68f036071382ea3029818fc909ef78a2c07a5ab382e6c4d0d3`。Release 资产发布成功后 workflow 已推进 `latest`/`stable`，Registry API 已确认 `0.1.8`、`stable`、`latest` 三个 tag 当前均指向该 digest；可移动 tag 仍只作为发现通道，生产安装、升级、回滚与下一候选的相邻版本门禁继续以 release manifest / `release-baseline.json` 中的完整不可变 digest 为准。
