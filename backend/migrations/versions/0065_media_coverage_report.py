"""辅种覆盖率报告：扫描运行、影片与种子证据三表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065_media_coverage_report"
down_revision: str | None = "0064_torrent_yield_daily"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_coverage_run",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("scope_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("scope_label", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="RUNNING"),
        sa.Column("phase", sa.String(length=32), nullable=False, server_default="PENDING"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("movie_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("site_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("watched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("identity_resolved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("degraded_sites", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index(
        "ix_media_coverage_run_started", "media_coverage_run", ["started_at"], unique=False
    )

    op.create_table(
        "media_coverage_movie",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("media_coverage_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("movie_key", sa.String(length=128), nullable=False),
        sa.Column("display_title", sa.String(length=512), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("resolution", sa.String(length=16), nullable=True),
        sa.Column("external_ids", sa.JSON(), nullable=False),
        sa.Column("relative_path_hint", sa.Text(), nullable=True),
        sa.Column("coverage_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("site_keys", sa.JSON(), nullable=False),
        sa.Column("bucket", sa.String(length=16), nullable=False),
        sa.Column("match_level", sa.String(length=24), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.Column("suggestion", sa.String(length=24), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.UniqueConstraint("run_id", "movie_key", name="uq_media_coverage_movie_key"),
    )
    op.create_index(
        "ix_media_coverage_movie_bucket",
        "media_coverage_movie",
        ["run_id", "bucket"],
        unique=False,
    )

    op.create_table(
        "media_coverage_torrent",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("media_coverage_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("movie_key", sa.String(length=128), nullable=False),
        sa.Column("downloader_id", sa.String(length=36), nullable=True),
        sa.Column("torrent_hash", sa.String(length=128), nullable=False),
        sa.Column("site_key", sa.String(length=64), nullable=True),
        sa.Column("remote_torrent_id", sa.String(length=128), nullable=True),
        sa.Column("match_level", sa.String(length=24), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "run_id", "movie_key", "torrent_hash", name="uq_media_coverage_torrent"
        ),
    )


def downgrade() -> None:
    op.drop_table("media_coverage_torrent")
    op.drop_index("ix_media_coverage_movie_bucket", table_name="media_coverage_movie")
    op.drop_table("media_coverage_movie")
    op.drop_index("ix_media_coverage_run_started", table_name="media_coverage_run")
    op.drop_table("media_coverage_run")
