"""v1.0.15 数据拆包 v2 基础模型。

测试阶段重构：本迁移先建立新的 unpack_* 模型。旧 task_definition/task_execution
写路径在新 API 切换完成前暂时保留，最终版本会删除旧拆包模型，不做历史拆包数据迁移。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_unpack_v2_foundation"
down_revision: str | None = "0034_cookiecloud_cron_v1010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "unpack_definition",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("trigger_kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("execution_scope_kind", sa.String(length=32), nullable=False),
        sa.Column("source_config", sa.JSON(), nullable=False),
        sa.Column("file_filter", sa.JSON(), nullable=False),
        sa.Column("site_ids", sa.JSON(), nullable=False),
        sa.Column("output_config", sa.JSON(), nullable=False),
        sa.Column("retry_enabled", sa.Boolean(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("auto_match_threshold_bps", sa.Integer(), nullable=False),
        sa.Column("cron_expression", sa.String(length=160), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "trigger_kind IN ('MANUAL', 'MONITOR')",
            name=op.f("ck_unpack_definition_trigger_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING_EXECUTION', 'ENABLED', 'PAUSED', 'ERROR')",
            name=op.f("ck_unpack_definition_status"),
        ),
        sa.CheckConstraint(
            "source_kind IN ('DIRECTORY', 'DOWNLOADER')",
            name=op.f("ck_unpack_definition_source_kind"),
        ),
        sa.CheckConstraint(
            "execution_scope_kind IN ('ALL_MATCHING_MEDIA', 'SELECTED_MEDIA')",
            name=op.f("ck_unpack_definition_execution_scope_kind"),
        ),
        sa.CheckConstraint(
            "max_retries BETWEEN 0 AND 10",
            name=op.f("ck_unpack_definition_max_retries"),
        ),
        sa.CheckConstraint(
            "auto_match_threshold_bps BETWEEN 0 AND 10000",
            name=op.f("ck_unpack_definition_auto_match_threshold_bps"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_unpack_definition_version_positive")),
        sa.CheckConstraint(
            "(trigger_kind = 'MONITOR') OR source_kind = 'DIRECTORY'",
            name=op.f("ck_unpack_definition_manual_directory_only"),
        ),
        sa.CheckConstraint(
            "(execution_scope_kind != 'SELECTED_MEDIA') OR "
            "(trigger_kind = 'MANUAL' AND source_kind = 'DIRECTORY')",
            name=op.f("ck_unpack_definition_selected_scope_manual_directory"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unpack_definition")),
    )
    op.create_index(
        "ix_unpack_definition_status_updated",
        "unpack_definition",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_unpack_definition_trigger_status",
        "unpack_definition",
        ["trigger_kind", "status"],
    )

    op.create_table(
        "unpack_definition_selected_source",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("definition_id", sa.String(length=36), nullable=False),
        sa.Column("source_object_key", sa.String(length=512), nullable=False),
        sa.Column("canonical_path_hint", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("size_bytes_at_selection", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["definition_id"],
            ["unpack_definition.id"],
            name=op.f("fk_unpack_definition_selected_source_definition_id_unpack_definition"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unpack_definition_selected_source")),
        sa.UniqueConstraint(
            "definition_id",
            "source_object_key",
            name="uq_unpack_definition_selected_source",
        ),
    )
    op.create_index(
        "ix_unpack_selected_source_definition",
        "unpack_definition_selected_source",
        ["definition_id"],
    )

    op.create_table(
        "unpack_execution",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("definition_id", sa.String(length=36), nullable=False),
        sa.Column("trigger", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column("discovery_cursor", sa.Text(), nullable=True),
        sa.Column("discovery_complete", sa.Boolean(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("matched_auto_count", sa.Integer(), nullable=False),
        sa.Column("review_count", sa.Integer(), nullable=False),
        sa.Column("content_verified_count", sa.Integer(), nullable=False),
        sa.Column("content_mismatch_count", sa.Integer(), nullable=False),
        sa.Column("timeout_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("completed_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "trigger IN ('MANUAL', 'SCHEDULE', 'MONITOR_EVENT')",
            name=op.f("ck_unpack_execution_trigger"),
        ),
        sa.CheckConstraint(
            "status IN ('DISCOVERING', 'MATCHING', 'REVIEW_REQUIRED', "
            "'CONTENT_VERIFYING', 'EXECUTING', 'CLIENT_VERIFYING', 'COMPLETED', "
            "'COMPLETED_WITH_ERRORS', 'PAUSED', 'FAILED', 'CANCELLED')",
            name=op.f("ck_unpack_execution_status"),
        ),
        sa.CheckConstraint("total_count >= 0", name=op.f("ck_unpack_execution_total_count")),
        sa.CheckConstraint(
            "matched_auto_count >= 0", name=op.f("ck_unpack_execution_matched_auto_count")
        ),
        sa.CheckConstraint("review_count >= 0", name=op.f("ck_unpack_execution_review_count")),
        sa.CheckConstraint(
            "content_verified_count >= 0",
            name=op.f("ck_unpack_execution_content_verified_count"),
        ),
        sa.CheckConstraint(
            "content_mismatch_count >= 0",
            name=op.f("ck_unpack_execution_content_mismatch_count"),
        ),
        sa.CheckConstraint("timeout_count >= 0", name=op.f("ck_unpack_execution_timeout_count")),
        sa.CheckConstraint("error_count >= 0", name=op.f("ck_unpack_execution_error_count")),
        sa.CheckConstraint(
            "completed_count >= 0", name=op.f("ck_unpack_execution_completed_count")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_unpack_execution_version_positive")),
        sa.ForeignKeyConstraint(
            ["definition_id"],
            ["unpack_definition.id"],
            name=op.f("fk_unpack_execution_definition_id_unpack_definition"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unpack_execution")),
    )
    op.create_index(
        "ix_unpack_execution_definition_started",
        "unpack_execution",
        ["definition_id", "started_at"],
    )
    op.create_index(
        "ix_unpack_execution_status_updated",
        "unpack_execution",
        ["status", "updated_at"],
    )

    op.create_table(
        "unpack_execution_item",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("source_object_key", sa.String(length=512), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("media_identity", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("selected_candidate_id", sa.String(length=36), nullable=True),
        sa.Column("candidate_generation", sa.Integer(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("content_verification_level", sa.String(length=32), nullable=True),
        sa.Column("torrent_metainfo_digest", sa.String(length=64), nullable=True),
        sa.Column("auxiliary_state", sa.JSON(), nullable=True),
        sa.Column("last_error_code", sa.String(length=96), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("match_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("match_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('DISCOVERED', 'MATCH_PENDING', 'MATCHING', 'MATCHED_AUTO', "
            "'REVIEW_REQUIRED', 'MATCHED_MANUAL', 'NO_MATCH', 'MATCH_TIMEOUT', "
            "'MATCH_ERROR', 'TORRENT_FETCHING', 'AUXILIARY_FETCHING', "
            "'CONTENT_VERIFYING', 'CONTENT_VERIFIED', 'CONTENT_MISMATCH', "
            "'PLAN_PENDING', 'EXECUTING', 'CLIENT_VERIFYING', 'COMPLETED', "
            "'EXECUTION_ERROR', 'CANCELLED')",
            name=op.f("ck_unpack_execution_item_status"),
        ),
        sa.CheckConstraint(
            "content_verification_level IS NULL OR content_verification_level IN "
            "('FULL_VERIFIED', 'CLIENT_CHECK_REQUIRED', 'BLOCKED')",
            name=op.f("ck_unpack_execution_item_content_verification_level"),
        ),
        sa.CheckConstraint(
            "candidate_generation >= 0",
            name=op.f("ck_unpack_execution_item_candidate_generation"),
        ),
        sa.CheckConstraint("retry_count >= 0", name=op.f("ck_unpack_execution_item_retry_count")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_unpack_execution_item_version_positive")),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["unpack_execution.id"],
            name=op.f("fk_unpack_execution_item_execution_id_unpack_execution"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unpack_execution_item")),
        sa.UniqueConstraint(
            "execution_id",
            "source_object_key",
            name="uq_unpack_execution_item_source",
        ),
    )
    op.create_index(
        "ix_unpack_item_execution_status",
        "unpack_execution_item",
        ["execution_id", "status", "id"],
    )

    op.create_table(
        "unpack_match_candidate",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_key", sa.String(length=255), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("imdb_id", sa.String(length=32), nullable=True),
        sa.Column("douban_id", sa.String(length=32), nullable=True),
        sa.Column("seeders", sa.Integer(), nullable=True),
        sa.Column("score_bps", sa.Integer(), nullable=False),
        sa.Column("is_exact_match", sa.Boolean(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("verification_status", sa.String(length=24), nullable=False),
        sa.Column("verification_level", sa.String(length=32), nullable=True),
        sa.Column("verification_error_code", sa.String(length=96), nullable=True),
        sa.Column("metainfo_digest", sa.String(length=64), nullable=True),
        sa.Column("raw_ref", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 0", name=op.f("ck_unpack_match_candidate_generation")),
        sa.CheckConstraint(
            "score_bps BETWEEN 0 AND 10000",
            name=op.f("ck_unpack_match_candidate_score_bps"),
        ),
        sa.CheckConstraint(
            "verification_status IN ('NOT_CHECKED', 'VERIFYING', 'VERIFIED', "
            "'MISMATCH', 'UNAVAILABLE')",
            name=op.f("ck_unpack_match_candidate_verification_status"),
        ),
        sa.CheckConstraint(
            "verification_level IS NULL OR verification_level IN "
            "('FULL_VERIFIED', 'CLIENT_CHECK_REQUIRED', 'BLOCKED')",
            name=op.f("ck_unpack_match_candidate_verification_level"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["unpack_execution_item.id"],
            name=op.f("fk_unpack_match_candidate_item_id_unpack_execution_item"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unpack_match_candidate")),
        sa.UniqueConstraint(
            "item_id",
            "generation",
            "site_id",
            "candidate_key",
            name="uq_unpack_candidate_key",
        ),
    )
    op.create_index(
        "ix_unpack_candidate_item_generation_score",
        "unpack_match_candidate",
        ["item_id", "generation", "score_bps"],
    )

    op.create_table(
        "unpack_review_decision",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=True),
        sa.Column("item_version_before", sa.Integer(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('APPROVE', 'NO_MATCH')",
            name=op.f("ck_unpack_review_decision_decision"),
        ),
        sa.CheckConstraint("generation >= 0", name=op.f("ck_unpack_review_decision_generation")),
        sa.CheckConstraint(
            "item_version_before >= 1",
            name=op.f("ck_unpack_review_decision_item_version_before"),
        ),
        sa.CheckConstraint(
            "(decision = 'APPROVE' AND candidate_id IS NOT NULL) OR "
            "(decision = 'NO_MATCH' AND candidate_id IS NULL)",
            name=op.f("ck_unpack_review_decision_candidate_binding"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["unpack_match_candidate.id"],
            name=op.f("fk_unpack_review_decision_candidate_id_unpack_match_candidate"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["unpack_execution_item.id"],
            name=op.f("fk_unpack_review_decision_item_id_unpack_execution_item"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unpack_review_decision")),
    )
    op.create_index(
        "ix_unpack_review_item_decided",
        "unpack_review_decision",
        ["item_id", "decided_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_unpack_review_item_decided", table_name="unpack_review_decision")
    op.drop_table("unpack_review_decision")
    op.drop_index(
        "ix_unpack_candidate_item_generation_score",
        table_name="unpack_match_candidate",
    )
    op.drop_table("unpack_match_candidate")
    op.drop_index("ix_unpack_item_execution_status", table_name="unpack_execution_item")
    op.drop_table("unpack_execution_item")
    op.drop_index("ix_unpack_execution_status_updated", table_name="unpack_execution")
    op.drop_index("ix_unpack_execution_definition_started", table_name="unpack_execution")
    op.drop_table("unpack_execution")
    op.drop_index(
        "ix_unpack_selected_source_definition",
        table_name="unpack_definition_selected_source",
    )
    op.drop_table("unpack_definition_selected_source")
    op.drop_index("ix_unpack_definition_trigger_status", table_name="unpack_definition")
    op.drop_index("ix_unpack_definition_status_updated", table_name="unpack_definition")
    op.drop_table("unpack_definition")
