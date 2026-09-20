from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import scripts.release_manifest as release_manifest
from scripts.verify_published_release_assets import verify_published_release_assets

TAG = "v1.0.0"
REPOSITORY = "YYxiaoma/PackBreaker"
IMAGE = "ghcr.io/yyxiaoma/packbreaker"
DIGEST = "sha256:" + "a" * 64
COMMIT = "b" * 40


def _assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(release_manifest, "project_version", lambda: "1.0.0")
    folder = tmp_path / "synthetic-release"
    folder.mkdir()
    sbom = folder / "packbreaker-1.0.0.spdx.json"
    sbom.write_text('{"spdxVersion":"SPDX-2.3","name":"synthetic"}\n', encoding="utf-8")
    manifest = release_manifest.build_release_manifest(
        tag=TAG,
        image=IMAGE,
        image_digest=DIGEST,
        commit=COMMIT,
        sbom_path=sbom,
        generated_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    (folder / "packbreaker-1.0.0.release.json").write_text(json.dumps(manifest), encoding="utf-8")
    files = sorted(folder.glob("*.json"))
    (folder / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {file.name}\n" for file in files
        ),
        encoding="ascii",
    )
    return folder


class _FakeGh:
    def __init__(self, folder: Path):
        self.folder = folder
        self.commands: list[list[str]] = []
        self.metadata: dict[str, Any] = {
            "tagName": TAG,
            "isDraft": False,
            "isPrerelease": False,
            "assets": [
                {"name": file.name, "size": file.stat().st_size}
                for file in sorted(folder.iterdir())
            ],
        }
        self.fail_download = False

    def run(self, command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        assert options == {
            "check": True,
            "capture_output": True,
            "text": True,
            "timeout": 60 if "view" in command else 120,
        }
        assert command[:3] in (["gh", "release", "view"], ["gh", "release", "download"])
        assert command[3:6] == [TAG, "--repo", REPOSITORY]
        if command[2] == "view":
            assert command[6:] == ["--json", "tagName,isDraft,isPrerelease,assets"]
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(self.metadata))
        assert command[6] == "--dir"
        if self.fail_download:
            raise subprocess.CalledProcessError(1, command)
        target = Path(command[7])
        for file in self.folder.iterdir():
            shutil.copyfile(file, target / file.name)
        return subprocess.CompletedProcess(command, 0, stdout="")


def _verify(fake: _FakeGh, **overrides: str) -> None:
    args = {
        "tag": TAG,
        "repository": REPOSITORY,
        "image": IMAGE,
        "image_digest": DIGEST,
        "commit": COMMIT,
    }
    args.update(overrides)
    verify_published_release_assets(run=fake.run, **args)


def test_published_release_assets_readback_checks_exact_files_and_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeGh(_assets(tmp_path, monkeypatch))
    _verify(fake)
    assert [command[2] for command in fake.commands] == ["view", "download"]


@pytest.mark.parametrize(
    "change",
    ["wrong-tag", "draft", "prerelease", "missing", "extra", "duplicate", "bad-size", "bool-size"],
)
def test_published_release_assets_rejects_bad_release_metadata_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    fake = _FakeGh(_assets(tmp_path, monkeypatch))
    if change == "wrong-tag":
        fake.metadata["tagName"] = "v0.1.9"
    elif change == "draft":
        fake.metadata["isDraft"] = True
    elif change == "prerelease":
        fake.metadata["isPrerelease"] = True
    elif change == "missing":
        fake.metadata["assets"].pop()
    elif change == "extra":
        fake.metadata["assets"].append({"name": "untrusted.bin", "size": 1})
    elif change == "duplicate":
        fake.metadata["assets"][1] = fake.metadata["assets"][0].copy()
    elif change == "bad-size":
        fake.metadata["assets"][0]["size"] = 0
    elif change == "bool-size":
        fake.metadata["assets"][0]["size"] = True
    with pytest.raises(ValueError):
        _verify(fake)
    assert [command[2] for command in fake.commands] == ["view"]


@pytest.mark.parametrize(
    "problem",
    ["tamper", "extra-file", "size-mismatch", "wrong-digest", "wrong-commit", "download-failure"],
)
def test_published_release_assets_rejects_uploaded_content_and_download_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    fake = _FakeGh(_assets(tmp_path, monkeypatch))
    kwargs: dict[str, str] = {}
    if problem == "tamper":
        path = fake.folder / "packbreaker-1.0.0.spdx.json"
        path.write_bytes(path.read_bytes().replace(b"synthetic", b"untrusted"))
        fake.metadata["assets"] = [
            {"name": file.name, "size": file.stat().st_size}
            for file in sorted(fake.folder.iterdir())
        ]
    elif problem == "extra-file":
        (fake.folder / "unexpected").write_bytes(b"untrusted")
    elif problem == "size-mismatch":
        fake.metadata["assets"][0]["size"] += 1
    elif problem == "wrong-digest":
        kwargs["image_digest"] = "sha256:" + "c" * 64
    elif problem == "wrong-commit":
        kwargs["commit"] = "c" * 40
    elif problem == "download-failure":
        fake.fail_download = True
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        _verify(fake, **kwargs)


@pytest.mark.parametrize(
    ("tag", "repo"), [("invalid", REPOSITORY), (TAG, "bad/repo/extra"), (TAG, "")]
)
def test_published_release_assets_rejects_unsafe_inputs_before_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tag: str, repo: str
) -> None:
    fake = _FakeGh(_assets(tmp_path, monkeypatch))
    with pytest.raises(ValueError, match="格式无效"):
        _verify(fake, tag=tag, repository=repo)
    assert fake.commands == []
