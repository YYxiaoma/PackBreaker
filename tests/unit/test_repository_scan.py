from pathlib import Path

from scripts.repository_scan import scan_paths


def test_repository_scan_accepts_normal_source_files(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("token_name = 'not-a-secret'\n")

    assert scan_paths(tmp_path, [source], max_file_bytes=1024) == []


def test_repository_scan_blocks_sensitive_artifacts_and_large_files(tmp_path: Path) -> None:
    torrent = tmp_path / "real.torrent"
    torrent.write_bytes(b"synthetic")
    large = tmp_path / "large.bin"
    large.write_bytes(b"x" * 11)

    findings = scan_paths(tmp_path, [torrent, large], max_file_bytes=10)

    assert {(item.path, item.rule) for item in findings} == {
        ("real.torrent", "blocked-artifact"),
        ("large.bin", "large-file>10"),
    }


def test_repository_scan_detects_private_key_and_known_token_shapes(tmp_path: Path) -> None:
    private_key = tmp_path / "fixture.txt"
    private_key.write_bytes(b"-----BEGIN " + b"PRIVATE KEY-----\nnot-real\n")
    token = tmp_path / "token.txt"
    token.write_text("ghp_" + "A" * 36)

    findings = scan_paths(tmp_path, [private_key, token])

    assert {(item.path, item.rule) for item in findings} == {
        ("fixture.txt", "private-key"),
        ("token.txt", "github-token"),
    }
