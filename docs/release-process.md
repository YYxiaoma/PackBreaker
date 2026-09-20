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

以下为 **v1.0.0 发布前的 v0.1.9 历史证据**。Release workflow run [`35429394091`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35429394091) 已成功完成 tag/version、上一正式版 `v0.1.8` baseline、全量质量门、`v0.1.8 → v0.1.9 → v0.1.8` 真实 Docker 升级/回滚、真实 updater helper 升级与自动回滚 E2E，并发布 linux/amd64 GHCR 镜像、release manifest、SPDX SBOM、`SHA256SUMS` 与 [GitHub Release](https://github.com/YYxiaoma/PackBreaker/releases/tag/v0.1.9) 资产。release 身份绑定 commit `cc70391cb42adc8755637d1cf23d407902e30dfe` 和不可变 digest `sha256:3bf7eee3825821a5fef28338b7f1ce749cbae353bcd3a4ef81883ee6a021261d`。在 v0.1.9 发布时，已独立核验三份资产哈希与 GHCR `0.1.9`、`stable`、`latest` 三个 tag 均指向其 digest；v1.0.0 发布后 `stable/latest` 已前移，仅 `0.1.9` 保留原摘要。可移动 tag 只作为发现通道，生产安装、升级、回滚与下一候选的相邻版本门禁继续以完整不可变 digest 为准。

2026-09-20 v1.0.0 待发布候选 `90572a9920ec6c79b1466d5cc8f52062a4a28b40` 已通过 [CI run `35503783297`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35503783297) 与 [Candidate Docker E2E run `35503783376`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35503783376)：包括原生 ARM64 Docker 构建/启动/备份及合成基线升级回滚、隔离真实下载器 API、浏览器审批链，以及正式 v0.1.9 GHCR 不可变 AMD64 基线身份/隔离启动、`v0.1.9 → 本地候选 → v0.1.9` 真实 Docker 升级回滚和 updater/Compose E2E。另从已发布的 v0.1.9 Release 重新下载 manifest、SPDX SBOM、SHA256SUMS，验证三份文件的实际大小及 SHA256 与 GitHub 资产元数据匹配，`scripts/verify_release_evidence.py` 对基线 tag/commit/image digest 的交叉核验通过。上述 CI 使用**本地候选镜像**，并未推送 v1.0.0 正式双架构 GHCR index；v1.0.0 正式 index/子镜像/双原生架构的**同一发布 digest**、发布后资产回读，以及该已推送 digest 的 AMD64 相邻升级回滚仍待正式 Release workflow 取得证据。未创建 v1.0.0 正式 Tag 或 Release，不得提前推进 stable/latest。

## 6. v1.0.0 正式发布中断记录（2026-09-20）

- `v1.0.0` 注释 Tag 已创建，指向 `ce86939224301315092b27621830560bf8438d58`；此前该提交的 [CI run `35504414122`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35504414122) 五个 Job 全部通过。该 Tag 已占用，不能未经明确批准重建、移动或用相同版本重推镜像。
- [Release run `35504683200`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35504683200) **失败**：原生 ARM64 前置测试、全量质量门、正式 v0.1.9 → 本地候选 → v0.1.9 升级回滚、真实 updater、双架构镜像推送、GHCR 平台/子配置检查、已推送不可变摘要的 AMD64 容器运行与正式 AMD64 相邻升级回滚已通过；随后 `Exercise immutable published ARM64 image under QEMU in isolated runtime` 步骤退出码为 1。原生 ARM64 对**已推送摘要**的运行 Job 和 Release 资产 Job 因依赖失败而跳过，因此不能声称 v1.0.0 正式发行成功。匿名读取 Job 原始日志返回 HTTP 403，当前尚未判明 QEMU 步骤失败的具体原因，严禁将推测当成故障根因。
- 已只读回读公开 GHCR：`ghcr.io/yyxiaoma/packbreaker:1.0.0` 的 OCI index digest 为 `sha256:fce1f3e3c0f56a8c024bbbf46a65fbc4ca51225a5c2bc2543fdddbb02c21a0c4`，存在 `linux/amd64` 子镜像 `sha256:a3c39fe8a34e6f4dd9501410a74e21a62f606ee85b6f9cf318f39d8b91af7de2` 与 `linux/arm64` 子镜像 `sha256:faa23abe3009b1c2f770677e7effeb7a1e3424f5182d0d0ad492ce3ecb9ee8ec`。两项配置 blob 的实际 OS/CPU、版本 1.0.0、修订标签均与索引及提交一致；**配置正确不等于 ARM64 正式运行通过**。
- GitHub API 没有 `v1.0.0` Release，最新正式 Release 仍为 `v0.1.9`；公开 GHCR `stable`、`latest` 和 `0.1.9` 仍指向上一正式 digest `sha256:3bf7eee3825821a5fef28338b7f1ce749cbae353bcd3a4ef81883ee6a021261d`。禁止把已推送但验收失败的 v1.0.0 index 作为生产部署身份或自动升级目标；`release-baseline.json` 继续固定 v0.1.9。
- 恢复发布前先取得 run `35504683200` / job `106062685717` 的失败步骤原始日志，在受控 CI 中复现并修复；再以**新的不可变版本身份**重新执行完整发行门禁，或另行制定经审查且不覆盖既有版本的恢复流程。不能简单重跑原工作流：它会拒绝已存在的 `:1.0.0` 镜像。

## 7. 相同不可变摘要的独立诊断与受控恢复

[Immutable ARM64 Diagnostics run `35506113046`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35506113046) 已对原索引 digest 在**原生 ARM64 和 QEMU** 两个独立 Runner 上完成隔离复测，两个 Job 均通过；QEMU 的镜像拉取、Python 执行、创建/启动、readiness、预检、备份和原始 `check-immutable-image-runtime.sh` 全部通过。该证据确认原摘要具备两架构运行能力，但无法从退出码 1 推导首次失败根因，亦不能把失败的原 Release run 事后改记为通过。

专用 `.github/workflows/immutable-v100-recovery.yml` 以**已存在的 Tag `v1.0.0` 和 index digest** 为只读镜像输入，重新验证远端 Tag 指向、版本身份、上一正式基线、实际双架构子配置、AMD64 正式摘要运行与相邻升级/回滚、QEMU 和原生 ARM64 正式摘要运行。仅当上述三个独立 Job 均通过且 GitHub Release 仍不存在时，资产 Job 才可针对**同一个 digest、同一 Tag、同一原始提交**生成 SBOM/manifest/SHA256SUMS、上传 Release、下载回读校验并最终推进 `stable/latest`。其代码不得构建、覆盖 `:1.0.0` 或移动 `v1.0.0` Tag；任何验证失败均停止资产发布。原始失败和恢复运行必须分别留档，不混写为同一次成功的 Release run。

首次 [受控恢复 run `35506579423`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35506579423) 在一个 AMD64 Runner 上依次运行 AMD64 原始摘要启动、v0.1.9→原始摘要→v0.1.9 升级回滚、ARM64 QEMU 原始运行脚本：前两项成功，QEMU 随后约 6 秒内失败；另一个独立原生 ARM64 Job 成功，资产 Job 依赖失败而跳过。与此前在独立干净 QEMU Runner 上通过的诊断相比较，**共享同一 Docker Runner 先执行 AMD64 再执行 QEMU** 是可疑的环境差异；目前没有原始完整报错，不得将其断言为已确诊的缓存故障。

恢复工作流已改为三个独立 Runner（AMD64 正式相邻升级/回滚、仅使用 ARM64 的干净 QEMU、原生 ARM64），三者均检查原始 index digest，并由资产 Job 同时依赖三个结果。该隔离消除不同架构的本地镜像引用/缓存相互影响这一潜在干扰，不省略任何正式运行或回滚校验；第一次失败的恢复记录仍保留。

[隔离恢复 run `35506873333`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35506873333) 的三个独立运行 Job 均成功，资产 Job 在上传前的本地一致性核对失败，未创建 GitHub Release 或推进通道。原因已用独立临时资产复现：恢复工作流未给 `release_manifest.py` 指定 `--generated-at`，生成的 UTC 时间含微秒，而 `verify_release_evidence.py` 要求到秒的 `YYYY-MM-DDTHH:MM:SSZ`。恢复工作流现与原正式发布流水线一致，显式传入 `date -u +%Y-%m-%dT%H:%M:%SZ`；修正后的相同生成与校验流程已在临时目录回归通过，不修改发布校验器或已有镜像。

## 8. v1.0.0 正式发布完成（2026-09-20）

- [最终受控恢复发布 run `35507181886`](https://github.com/YYxiaoma/PackBreaker/actions/runs/35507181886) 的三个独立 Job 均成功：已推送不可变摘要 AMD64 原生运行及正式 v0.1.9→v1.0.0→v0.1.9 相邻升级/回滚、QEMU ARM64 完整运行、原生 ARM64 完整运行。原始 Tag 始终指向 `ce86939224301315092b27621830560bf8438d58`，未重建或覆盖 `:1.0.0` 镜像。
- 同一运行的资产 Job 已通过本地校验、上传 [v1.0.0 GitHub Release](https://github.com/YYxiaoma/PackBreaker/releases/tag/v1.0.0)、重新下载回读及 `stable/latest` 推进。已另从公开 Release 独立下载并以 `scripts/verify_release_evidence.py` 再次校验三份实际资产，退出码为 0：Release JSON SHA256 `a9e7e4fbc25c0d40e7a8440d5b1abd7866410a016287fb929204f179f6318da2`；SPDX SBOM SHA256 `72aa07918de62022b463672528d42198d8477168cf0c87860134a1cf64227444`；SHA256SUMS SHA256 `a21801bc47e28d7a953e21cf15f7aadc83deb10f912ee9246393879801ac217a`。
- 已独立从 GHCR 回读 `:1.0.0`、`:stable`、`:latest`，三者 OCI index digest 均为 `sha256:fce1f3e3c0f56a8c024bbbf46a65fbc4ca51225a5c2bc2543fdddbb02c21a0c4`，实际 index 内容 SHA256 与 Registry digest 一致且包含 `linux/amd64`、`linux/arm64`。旧版 `:0.1.9` 仍保持其原 digest `sha256:3bf7eee3825821a5fef28338b7f1ce749cbae353bcd3a4ef81883ee6a021261d`。
- `release-baseline.json` 已切换为正式 v1.0.0 的双架构不可变摘要，供后续研发候选的相邻版本升级/回滚使用。首次失败的原 Release run 和两次失败的恢复尝试均保留为历史失败证据，不将其改写成成功；正式完成以最终恢复 run `35507181886` 为准。
