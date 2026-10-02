from pathlib import Path

from backend.app.movie_dedup_acceptance import run_acceptance


def test_movie_dedup_acceptance_script_same_filesystem(tmp_path: Path) -> None:
    source_parent = tmp_path / "source-parent"
    target_parent = tmp_path / "target-parent"
    source_parent.mkdir()
    target_parent.mkdir()

    result = run_acceptance(
        data_root=tmp_path,
        source_parent=source_parent,
        target_parent=target_parent,
    )

    assert result["same_filesystem"] is True
    assert result["auto_stop"] == "HARDLINK_COMMITTED"
    assert result["hardlink_inode_equal"] is True
    assert result["same_size_different_content"] == "BLOCKED_NO_MUTATION"
    assert result["fixture_cleanup"] == "COMPLETED"
