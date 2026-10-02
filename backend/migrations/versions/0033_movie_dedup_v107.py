"""增加 v1.0.7 影片去重任务、清单、候选关系与安全操作 journal。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_movie_dedup_v107"
down_revision: str | None = "0032_cookiecloud_v105"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "movie_dedup_job",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("source_root", sa.Text(), nullable=False),
        sa.Column("target_root", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("cross_filesystem_policy", sa.String(length=16), nullable=False),
        sa.Column("include_subdirectories", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("min_size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("video_extensions", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("source_scan_cursor", sa.Text(), nullable=True),
        sa.Column("target_scan_cursor", sa.Text(), nullable=True),
        sa.Column("source_file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("target_file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verified_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deduplicated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("logical_duplicate_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "estimated_reclaimable_bytes", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("trace_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("mode IN ('AUTO', 'HARDLINK', 'SYMLINK', 'SCAN_ONLY')", name="mode"),
        sa.CheckConstraint(
            "cross_filesystem_policy IN ('STOP', 'SYMLINK')", name="cross_filesystem_policy"
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'REVIEW_REQUIRED', 'COMPLETED', "
            "'PARTIAL_FAILED', 'FAILED', 'CANCELLED', 'RECOVERY_REQUIRED')",
            name="status",
        ),
        sa.CheckConstraint(
            "phase IN ('PENDING', 'SCANNING_SOURCE', 'SCANNING_TARGET', 'MATCHING', "
            "'VERIFYING', 'REVIEW', 'EXECUTING', 'FINAL_VERIFYING', 'COMPLETED', 'FAILED')",
            name="phase",
        ),
        sa.CheckConstraint("min_size_bytes >= 0", name="min_size_nonnegative"),
        sa.CheckConstraint("source_file_count >= 0", name="source_file_count_nonnegative"),
        sa.CheckConstraint("target_file_count >= 0", name="target_file_count_nonnegative"),
        sa.CheckConstraint("candidate_count >= 0", name="candidate_count_nonnegative"),
        sa.CheckConstraint("verified_count >= 0", name="verified_count_nonnegative"),
        sa.CheckConstraint("deduplicated_count >= 0", name="deduplicated_count_nonnegative"),
        sa.CheckConstraint("failed_count >= 0", name="failed_count_nonnegative"),
        sa.CheckConstraint(
            "logical_duplicate_bytes >= 0", name="logical_duplicate_bytes_nonnegative"
        ),
        sa.CheckConstraint(
            "estimated_reclaimable_bytes >= 0", name="estimated_reclaimable_bytes_nonnegative"
        ),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_movie_dedup_job"),
    )
    op.create_index(
        "ix_movie_dedup_job_status_updated",
        "movie_dedup_job",
        ["status", "updated_at"],
        unique=False,
    )

    op.create_table(
        "movie_dedup_file_inventory",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("basename", sa.Text(), nullable=False),
        sa.Column("extension", sa.String(length=32), nullable=False),
        sa.Column("device", sa.BigInteger(), nullable=False),
        sa.Column("inode", sa.BigInteger(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("allocated_bytes", sa.BigInteger(), nullable=False),
        sa.Column("link_count", sa.Integer(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("ctime_ns", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.Integer(), nullable=False),
        sa.Column("uid", sa.Integer(), nullable=False),
        sa.Column("gid", sa.Integer(), nullable=False),
        sa.Column("media_metadata", sa.JSON(), nullable=False),
        sa.Column("quick_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("full_sha256", sa.String(length=64), nullable=True),
        sa.Column("scan_status", sa.String(length=24), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("side IN ('A', 'B')", name="side"),
        sa.CheckConstraint(
            "scan_status IN ('DISCOVERED', 'MATCHED', 'QUICK_HASHED', 'FULL_HASHED', "
            "'SKIPPED', 'FAILED')",
            name="scan_status",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="size_nonnegative"),
        sa.CheckConstraint("allocated_bytes >= 0", name="allocated_nonnegative"),
        sa.CheckConstraint("link_count >= 1", name="link_count_positive"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["movie_dedup_job.id"],
            name="fk_movie_dedup_file_inventory_job_id_movie_dedup_job",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_movie_dedup_file_inventory"),
        sa.UniqueConstraint(
            "job_id", "side", "relative_path", name="uq_movie_dedup_inventory_path"
        ),
    )
    op.create_index(
        "ix_movie_dedup_inventory_job_side_size",
        "movie_dedup_file_inventory",
        ["job_id", "side", "size_bytes"],
        unique=False,
    )
    op.create_index(
        "ix_movie_dedup_inventory_full_sha256",
        "movie_dedup_file_inventory",
        ["job_id", "full_sha256"],
        unique=False,
    )

    op.create_table(
        "movie_dedup_pair",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("source_file_id", sa.String(length=36), nullable=False),
        sa.Column("target_file_id", sa.String(length=36), nullable=False),
        sa.Column("match_level", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("metadata_match", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("size_match", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("quick_hash_match", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("full_hash_match", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("resolved_action", sa.String(length=16), nullable=False),
        sa.Column(
            "estimated_reclaimable_bytes", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ('CANDIDATE', 'VERIFIED_DUPLICATE', 'REVIEW_REQUIRED', "
            "'ALREADY_DEDUPLICATED', 'READY', 'COMPLETED', 'BLOCKED', 'FAILED')",
            name="status",
        ),
        sa.CheckConstraint(
            "resolved_action IN ('HARDLINK', 'SYMLINK', 'SCAN_ONLY', 'BLOCKED')",
            name="resolved_action",
        ),
        sa.CheckConstraint(
            "estimated_reclaimable_bytes >= 0", name="estimated_reclaimable_bytes_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["movie_dedup_job.id"],
            name="fk_movie_dedup_pair_job_id_movie_dedup_job",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_file_id"],
            ["movie_dedup_file_inventory.id"],
            name="fk_movie_dedup_pair_source_file_id_inventory",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_file_id"],
            ["movie_dedup_file_inventory.id"],
            name="fk_movie_dedup_pair_target_file_id_inventory",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_movie_dedup_pair"),
        sa.UniqueConstraint(
            "job_id", "source_file_id", "target_file_id", name="uq_movie_dedup_pair_files"
        ),
    )
    op.create_index(
        "ix_movie_dedup_pair_job_status",
        "movie_dedup_pair",
        ["job_id", "status"],
        unique=False,
    )

    op.create_table(
        "movie_dedup_operation_journal",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("pair_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("operation_token", sa.String(length=64), nullable=False),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("target_snapshot", sa.JSON(), nullable=False),
        sa.Column("temporary_path", sa.Text(), nullable=True),
        sa.Column("temporary_snapshot", sa.JSON(), nullable=True),
        sa.Column("after_snapshot", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ('INTENT_RECORDED', 'TEMP_LINK_CREATED', 'EXCHANGED', 'VERIFIED', "
            "'COMMITTED', 'FAILED', 'RECOVERY_REQUIRED')",
            name="status",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["movie_dedup_job.id"],
            name="fk_movie_dedup_operation_journal_job_id_movie_dedup_job",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pair_id"],
            ["movie_dedup_pair.id"],
            name="fk_movie_dedup_operation_journal_pair_id_movie_dedup_pair",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_movie_dedup_operation_journal"),
        sa.UniqueConstraint("idempotency_key", name="uq_movie_dedup_journal_idempotency_key"),
    )
    op.create_index(
        "ix_movie_dedup_journal_job_status",
        "movie_dedup_operation_journal",
        ["job_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_movie_dedup_journal_pair_created",
        "movie_dedup_operation_journal",
        ["pair_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_movie_dedup_journal_pair_created", table_name="movie_dedup_operation_journal")
    op.drop_index("ix_movie_dedup_journal_job_status", table_name="movie_dedup_operation_journal")
    op.drop_table("movie_dedup_operation_journal")
    op.drop_index("ix_movie_dedup_pair_job_status", table_name="movie_dedup_pair")
    op.drop_table("movie_dedup_pair")
    op.drop_index("ix_movie_dedup_inventory_full_sha256", table_name="movie_dedup_file_inventory")
    op.drop_index("ix_movie_dedup_inventory_job_side_size", table_name="movie_dedup_file_inventory")
    op.drop_table("movie_dedup_file_inventory")
    op.drop_index("ix_movie_dedup_job_status_updated", table_name="movie_dedup_job")
    op.drop_table("movie_dedup_job")
