# 发布流程与供应链证据

## 1. 发布身份

PackBreaker 当前项目版本来自 `pyproject.toml`。正式发布 tag 必须精确等于 `v<project.version>`；`scripts/release_manifest.py --validate-tag-only` 会在构建前失败关闭任何版本不一致的 tag。

部署身份不是 `stable`、版本 tag 或 commit tag，而是发布清单中的：

`immutable_image = <ghcr-image>@sha256:<image-digest>`

`stable` 仅用于发现当前稳定通道，版本 tag 用于人类导航；生产安装、升级和回滚记录必须保存完整 digest。发布流水线会拒绝已存在的同版本 GitHub Release 或 GHCR 版本镜像，避免重跑覆盖不可变版本身份。

## 2. 基础镜像与目标平台

Dockerfile 的三个 `FROM` 都使用“精确补丁版本 + sha256 digest”固定基础镜像，避免构建时被同名可变 tag 悄然替换。v0.1.9 已正式发布的镜像仅含 `linux/amd64`。v1.0.0 研发分支的 Release workflow 已预设 `linux/amd64,linux/arm64` 双架构 Buildx 发布目标，并增加原生 ARM64 CI/发布前启动备份验证与最终镜像索引检查；在真实 ARM64 Docker 和升级/回滚验收完成前，该变更不代表已正式支持 ARM64。

更新基础镜像时必须显式修改 Dockerfile 中的 tag 和 digest，并重新通过容器、迁移、恢复与仓库安全门禁；不能只修改 tag 而省略 digest。

## 3. Tag 发布流水线

推送 `v*.*.*` tag 后，`.github/workflows/release.yml` 按以下顺序执行：

1. 安装冻结的 Python/Node 依赖，验证 tag 与项目版本一致，并拒绝已经存在的同 tag GitHub Release。
2. 校验 `release-baseline.json`，并通过 GitHub Releases API 强制其 tag 必须等于当前最新正式 Release；这样后续版本不能跳过直接上一正式版本的兼容门禁。
3. 运行 `scripts/check.py` 和 `scripts/test.py` 全量质量门禁。
4. 本地构建 release candidate，先执行“上一正式 release 不可变 digest → 当前候选 → 恢复升级前备份并回滚上一正式 digest”的兼容门禁，再执行独立 updater helper 的真实 Docker 成功升级与故障候选自动数据库/容器回滚 E2E；两条门禁都通过前不推送新正式镜像。
5. v0.x 继续构建单平台 `linux/amd64`；v1.0.0 起在原生 ARM64 验证通过后使用 Buildx 构建双架构镜像并推送 GHCR。按 registry 返回的不可变 index digest 检查实际平台列表，并逐个按索引子 digest 读取 `linux/amd64`、`linux/arm64` 子镜像 manifest，验证各子清单的格式、配置和层 descriptor 均有效；再按同一子 digest 通过 Buildx `.Image` 读取实际 image config，要求 OS/CPU 架构与 index 声明一致、rootfs 层数与 manifest 一致、OCI version/revision 标签与 workflow 独立传入的 Git Tag/Git SHA 一致。缺失、错误格式、重复 digest、无法读取子清单或 config 均失败关闭。此验证发生在生成 SBOM / GitHub Release 资产之前；本地离线测试只能证明检查逻辑，正式镜像尚需真实 GHCR index 读取和双宿主机运行验收。
6. 按已推送的 `image@sha256:<index-digest>` 在独立临时 `/config`、`/data` 中先启动 AMD64 原生容器与 QEMU ARM64 容器；v1.0.0 起，AMD64 还须使用**同一已推送不可变 digest**接管上一正式版本的合成数据，并用上一正式 digest 的备份恢复工具执行回滚与再次就绪验证，不能只用本地构建候选完成相邻版本兼容门禁。构建任务结束后，将同一个不可变 index digest 作为跨 Job 输出交给**原生 ARM64 Runner**，在独立临时卷、`--network none`、无宿主机端口条件下再次拉取并启动相同 digest 的 ARM64 子镜像，核对实际 CPU、镜像平台、版本/提交标签、内置版本、服务就绪、维护预检和数据库备份。原生 ARM64 结果未通过时，后续资产发布 Job 不得启动。v0.x 已发布版本仅为 AMD64，原生 ARM64 Job 仍成功执行空操作，而不是被整体跳过从而阻断 AMD64-only 历史发布。只有这些运行与相邻版本门禁全部成功才重新核对版本 tag 的 index digest、生成 SBOM 并发布资产；如候选 digest 已推送但验收失败，须保留其失败状态且不得把它当作已发布版本或推进 stable/latest。
7. 只按 `<image>@<digest>` 扫描镜像并生成 SPDX JSON SBOM。
8. 生成 `packbreaker-<version>.release.json`，绑定 version/tag/commit/platform（v1.0.0 起为 platforms）/image digest/SBOM 文件名与 SBOM SHA-256，并为 release JSON 与 SBOM 生成 `SHA256SUMS`。在任何资产上传之前运行 `scripts/verify_release_evidence.py`，以独立的 workflow 输入交叉核对完整不可变镜像身份、commit、tag、双平台声明、SPDX 版本、文件名与三份资产的校验和；缺失、重复、额外资产或符号链接均失败关闭。该本地一致性检查不能证明镜像已在两架构真实运行或 SBOM 内容由其正确生成，实际镜像平台仍以 GHCR index 检查和原生 ARM64 门禁为准。
9. 本地资产检查通过、原生 ARM64 对相同不可变 digest 的运行检查成功后，才上传 GitHub Actions evidence artifact，创建同 tag 的 GitHub Release，并发布 SBOM、release manifest、checksums 与自动 release notes。上传后通过 GitHub Release API 检查 tag、正式发布状态及三份资产名称/大小，再下载这三份**已发布资产**到全新临时目录，重用 `verify_release_evidence.py` 对独立 workflow tag/commit/image digest、SBOM 哈希与 SHA256SUMS 做只读复核。上传前本地检查不能代替上传后的实际资产复核；任一资产缺失、重复、变更或下载失败均不推进镜像通道。
10. 已发布资产只读复核通过，并再次确认 GitHub 最新正式 Release 仍为当前 tag 后，才把 `stable` 与兼容 Docker 默认习惯的 `latest` 通道一起移动到本次已经记录的不可变 image digest；更新后分别按 registry 返回的两个通道 digest 回读确认。若上传后复核或通道推进/回读失败，GitHub Release 可能已经存在，且通道可能处于未更新或部分更新状态；不得直接重跑已存在的不可变版本发布，须核对证据并按既定独立通道同步流程处理。较新的正式 Release 已出现时不得由旧工作流回退两个通道。

Buildx 同时开启 provenance 元数据，但当前 M6 不把它表述为独立签名或 artifact attestation；若后续启用签名/attestation，必须另行定义密钥、身份与验证策略。

若历史版本缺少 `stable`/`latest` 或可移动通道需要修复，可手工运行 `Sync Release Channels` workflow。该 workflow 只读取 `release-baseline.json`，并在确认 baseline tag 仍等于 GitHub 最新正式 Release 后，把两个通道同步到 baseline 的不可变 digest；它不会构建或重发镜像内容。

## 4. 发布产物

每个正式版本至少包含：

- GHCR 正式镜像及不可变 digest；v0.x 为 `linux/amd64`，v1.0.0 目标为 `linux/amd64` + `linux/arm64`；
- `packbreaker-<version>.spdx.json`；
- `packbreaker-<version>.release.json`；
- `SHA256SUMS`；
- GitHub Release notes；
- 与该版本对应的部署、升级兼容和已知限制文档。

release manifest 不保存凭证、数据库或真实环境路径。SBOM 从已经按 digest 推送的镜像生成，因此其输入与发布清单中的镜像身份一致。

## 5. 当前发布证据

当前最新正式版本为 `v0.1.9`。Release workflow run [`35429394091`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35429394091) 已成功完成 tag/version、上一正式版 `v0.1.8` baseline、全量质量门、`v0.1.8 → v0.1.9 → v0.1.8` 真实 Docker 升级/回滚、真实 updater helper 升级与自动回滚 E2E，并发布 linux/amd64 GHCR 镜像、release manifest、SPDX SBOM、`SHA256SUMS` 与 [GitHub Release](https://github.com/YYxiaoma/PackBreaker/releases/tag/v0.1.9) 资产。release 身份绑定 commit `cc70391cb42adc8755637d1cf23d407902e30dfe` 和不可变 digest `sha256:3bf7eee3825821a5fef28338b7f1ce749cbae353bcd3a4ef81883ee6a021261d`。已独立核验 Release 资产哈希与 GHCR `0.1.9`、`stable`、`latest` 三个 tag 均指向该 digest；可移动 tag 仍只作为发现通道，生产安装、升级、回滚与下一候选的相邻版本门禁继续以 release manifest / `release-baseline.json` 中的完整不可变 digest 为准。

2026-09-20 v1.0.0 待发布候选 `90572a9920ec6c79b1466d5cc8f52062a4a28b40` 已通过 [CI run `35503783297`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35503783297) 与 [Candidate Docker E2E run `35503783376`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35503783376)：包括原生 ARM64 Docker 构建/启动/备份及合成基线升级回滚、隔离真实下载器 API、浏览器审批链，以及正式 v0.1.9 GHCR 不可变 AMD64 基线身份/隔离启动、`v0.1.9 → 本地候选 → v0.1.9` 真实 Docker 升级回滚和 updater/Compose E2E。另从已发布的 v0.1.9 Release 重新下载 manifest、SPDX SBOM、SHA256SUMS，验证三份文件的实际大小及 SHA256 与 GitHub 资产元数据匹配，`scripts/verify_release_evidence.py` 对基线 tag/commit/image digest 的交叉核验通过。上述 CI 使用**本地候选镜像**，并未推送 v1.0.0 正式双架构 GHCR index；v1.0.0 正式 index/子镜像/双原生架构的**同一发布 digest**、发布后资产回读，以及该已推送 digest 的 AMD64 相邻升级回滚仍待正式 Release workflow 取得证据。未创建 v1.0.0 正式 Tag 或 Release，不得提前推进 stable/latest。
