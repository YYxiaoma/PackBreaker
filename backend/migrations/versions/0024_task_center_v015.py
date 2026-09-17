"""新增 v0.1.5 任务定义、调度、执行记录与结构化事件模型。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_task_center_v015"
down_revision: str | None = "0023_backup_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_definition",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("site_id", sa.String(length=36), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('MANUAL', 'MONITOR')", name=op.f("ck_task_definition_kind")),
        sa.CheckConstraint(
            "status IN ('ENABLED', 'PAUSED', 'SITE_UNAVAILABLE', 'ERROR')",
            name=op.f("ck_task_definition_status"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_task_definition_version_positive")),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["site.id"],
            name=op.f("fk_task_definition_site_id_site"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_definition")),
    )
    op.create_index("ix_task_definition_kind_status", "task_definition", ["kind", "status"])
    op.create_index("ix_task_definition_site_id", "task_definition", ["site_id"])

    op.create_table(
        "task_schedule",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_definition_id", sa.String(length=36), nullable=False),
        sa.Column("cron_expression", sa.String(length=160), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_successful_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scan_checkpoint", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_definition_id"],
            ["task_definition.id"],
            name=op.f("fk_task_schedule_task_definition_id_task_definition"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_schedule")),
        sa.UniqueConstraint("task_definition_id", name="uq_task_schedule_definition"),
    )
    op.create_index("ix_task_schedule_next_run_at", "task_schedule", ["next_run_at"])

    op.create_table(
        "task_source",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_definition_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("downloader_id", sa.String(length=36), nullable=True),
        sa.Column("directory_path", sa.Text(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('DOWNLOADER', 'DIRECTORY')", name=op.f("ck_task_source_kind")),
        sa.CheckConstraint(
            "(kind = 'DOWNLOADER' AND directory_path IS NULL) OR "
            "(kind = 'DIRECTORY' AND downloader_id IS NULL AND directory_path IS NOT NULL)",
            name=op.f("ck_task_source_binding"),
        ),
        sa.ForeignKeyConstraint(
            ["downloader_id"],
            ["downloader.id"],
            name=op.f("fk_task_source_downloader_id_downloader"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["task_definition_id"],
            ["task_definition.id"],
            name=op.f("fk_task_source_task_definition_id_task_definition"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_source")),
        sa.UniqueConstraint("task_definition_id", name="uq_task_source_definition"),
    )
    op.create_index("ix_task_source_downloader_id", "task_source", ["downloader_id"])

    op.create_table(
        "task_filter",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_definition_id", sa.String(length=36), nullable=False),
        sa.Column("file_types", sa.JSON(), nullable=False),
        sa.Column("video_extensions", sa.JSON(), nullable=False),
        sa.Column("archive_extensions", sa.JSON(), nullable=False),
        sa.Column("min_size_bytes", sa.Integer(), nullable=True),
        sa.Column("max_size_bytes", sa.Integer(), nullable=True),
        sa.Column("include_name", sa.String(length=512), nullable=True),
        sa.Column("exclude_names", sa.JSON(), nullable=False),
        sa.Column("ignore_temp_files", sa.Boolean(), nullable=False),
        sa.Column("temp_patterns", sa.JSON(), nullable=False),
        sa.Column("include_subdirectories", sa.Boolean(), nullable=False),
        sa.Column("max_scan_depth", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "min_size_bytes IS NULL OR min_size_bytes >= 0", name=op.f("ck_task_filter_min_size")
        ),
        sa.CheckConstraint(
            "max_size_bytes IS NULL OR max_size_bytes >= 0", name=op.f("ck_task_filter_max_size")
        ),
        sa.CheckConstraint(
            "min_size_bytes IS NULL OR max_size_bytes IS NULL OR min_size_bytes <= max_size_bytes",
            name=op.f("ck_task_filter_size_range"),
        ),
        sa.CheckConstraint(
            "max_scan_depth IS NULL OR max_scan_depth >= 0",
            name=op.f("ck_task_filter_max_scan_depth"),
        ),
        sa.ForeignKeyConstraint(
            ["task_definition_id"],
            ["task_definition.id"],
            name=op.f("fk_task_filter_task_definition_id_task_definition"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_filter")),
        sa.UniqueConstraint("task_definition_id", name="uq_task_filter_definition"),
    )

    op.create_table(
        "task_output_policy",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_definition_id", sa.String(length=36), nullable=False),
        sa.Column("output_directory", sa.Text(), nullable=False),
        sa.Column("storage_mode", sa.String(length=16), nullable=False),
        sa.Column("preserve_structure", sa.Boolean(), nullable=False),
        sa.Column("conflict_policy", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "storage_mode IN ('HARDLINK', 'SYMLINK', 'COPY')",
            name=op.f("ck_task_output_policy_storage_mode"),
        ),
        sa.CheckConstraint(
            "conflict_policy IN ('VERIFY_REUSE_OR_STOP', 'SKIP', 'RENAME', 'OVERWRITE')",
            name=op.f("ck_task_output_policy_conflict_policy"),
        ),
        sa.ForeignKeyConstraint(
            ["task_definition_id"],
            ["task_definition.id"],
            name=op.f("fk_task_output_policy_task_definition_id_task_definition"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_output_policy")),
        sa.UniqueConstraint("task_definition_id", name="uq_task_output_policy_definition"),
    )

    op.create_table(
        "task_execution_policy",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_definition_id", sa.String(length=36), nullable=False),
        sa.Column("stability_detection_enabled", sa.Boolean(), nullable=False),
        sa.Column("stability_wait_seconds", sa.Integer(), nullable=False),
        sa.Column("only_completed_downloads", sa.Boolean(), nullable=False),
        sa.Column("initial_scope", sa.String(length=24), nullable=False),
        sa.Column("debounce_seconds", sa.Integer(), nullable=False),
        sa.Column("overlap_policy", sa.String(length=24), nullable=False),
        sa.Column("auto_retry_enabled", sa.Boolean(), nullable=False),
        sa.Column("max_auto_retries", sa.Integer(), nullable=False),
        sa.Column("retry_intervals_seconds", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "initial_scope IN ('NEW_ONLY', 'INCLUDE_EXISTING')",
            name=op.f("ck_task_execution_policy_initial_scope"),
        ),
        sa.CheckConstraint(
            "overlap_policy IN ('SKIP', 'RUN_ONCE_AFTER')",
            name=op.f("ck_task_execution_policy_overlap_policy"),
        ),
        sa.CheckConstraint(
            "stability_wait_seconds >= 0", name=op.f("ck_task_execution_policy_stability_wait")
        ),
        sa.CheckConstraint("debounce_seconds >= 0", name=op.f("ck_task_execution_policy_debounce")),
        sa.CheckConstraint(
            "max_auto_retries BETWEEN 0 AND 20",
            name=op.f("ck_task_execution_policy_max_auto_retries"),
        ),
        sa.ForeignKeyConstraint(
            ["task_definition_id"],
            ["task_definition.id"],
            name=op.f("fk_task_execution_policy_task_definition_id_task_definition"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_execution_policy")),
        sa.UniqueConstraint("task_definition_id", name="uq_task_execution_policy_definition"),
    )

    op.create_table(
        "task_execution",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_definition_id", sa.String(length=36), nullable=True),
        sa.Column("task_name", sa.String(length=120), nullable=False),
        sa.Column("trigger", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("phase", sa.String(length=24), nullable=False),
        sa.Column("source_execution_id", sa.String(length=36), nullable=True),
        sa.Column("trace_id", sa.String(length=36), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column("discovered_count", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "trigger IN ('MANUAL', 'CRON', 'IMMEDIATE_SCAN', 'FAILED_RETRY', 'SYSTEM_RECOVERY')",
            name=op.f("ck_task_execution_trigger"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'PARTIAL_FAILED', "
            "'FAILED', 'CANCELLED')",
            name=op.f("ck_task_execution_status"),
        ),
        sa.CheckConstraint(
            "phase IN ('WAITING', 'DISCOVERING', 'ANALYZING', 'SCANNING_SITE', 'PREPARING', "
            "'UNPACKING', 'OUTPUTTING', 'VERIFYING', 'COMPLETED', 'FAILED', 'SKIPPED')",
            name=op.f("ck_task_execution_phase"),
        ),
        sa.CheckConstraint(
            "discovered_count >= 0", name=op.f("ck_task_execution_discovered_count")
        ),
        sa.CheckConstraint("success_count >= 0", name=op.f("ck_task_execution_success_count")),
        sa.CheckConstraint("failed_count >= 0", name=op.f("ck_task_execution_failed_count")),
        sa.CheckConstraint("skipped_count >= 0", name=op.f("ck_task_execution_skipped_count")),
        sa.ForeignKeyConstraint(
            ["source_execution_id"],
            ["task_execution.id"],
            name=op.f("fk_task_execution_source_execution_id_task_execution"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["task_definition_id"],
            ["task_definition.id"],
            name=op.f("fk_task_execution_task_definition_id_task_definition"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_execution")),
    )
    op.create_index(
        "ix_task_execution_definition_started",
        "task_execution",
        ["task_definition_id", "started_at"],
    )
    op.create_index("ix_task_execution_status_created", "task_execution", ["status", "created_at"])

    op.create_table(
        "task_execution_item",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("unpack_task_id", sa.String(length=36), nullable=True),
        sa.Column("source_object_key", sa.String(length=512), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("phase", sa.String(length=24), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=True),
        sa.Column("result", sa.String(length=24), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_summary_zh", sa.Text(), nullable=True),
        sa.Column("technical_detail", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "phase IN ('WAITING', 'DISCOVERING', 'ANALYZING', 'SCANNING_SITE', 'PREPARING', "
            "'UNPACKING', 'OUTPUTTING', 'VERIFYING', 'COMPLETED', 'FAILED', 'SKIPPED')",
            name=op.f("ck_task_execution_item_phase"),
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name=op.f("ck_task_execution_item_size"),
        ),
        sa.CheckConstraint(
            "progress IS NULL OR (progress >= 0 AND progress <= 100)",
            name=op.f("ck_task_execution_item_progress"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["task_execution.id"],
            name=op.f("fk_task_execution_item_execution_id_task_execution"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["unpack_task_id"],
            ["unpack_task.id"],
            name=op.f("fk_task_execution_item_unpack_task_id_unpack_task"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_execution_item")),
        sa.UniqueConstraint(
            "execution_id", "source_object_key", name="uq_task_execution_item_source"
        ),
    )
    op.create_index(
        "ix_task_execution_item_execution_phase", "task_execution_item", ["execution_id", "phase"]
    )

    op.create_table(
        "task_execution_event",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("event_code", sa.String(length=96), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.String(length=36), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["task_execution.id"],
            name=op.f("fk_task_execution_event_execution_id_task_execution"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_execution_event")),
    )
    op.create_index(
        "ix_task_execution_event_execution_created",
        "task_execution_event",
        ["execution_id", "created_at"],
    )
    op.create_index(
        "ix_task_execution_event_code_created", "task_execution_event", ["event_code", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_task_execution_event_code_created", table_name="task_execution_event")
    op.drop_index("ix_task_execution_event_execution_created", table_name="task_execution_event")
    op.drop_table("task_execution_event")
    op.drop_index("ix_task_execution_item_execution_phase", table_name="task_execution_item")
    op.drop_table("task_execution_item")
    op.drop_index("ix_task_execution_status_created", table_name="task_execution")
    op.drop_index("ix_task_execution_definition_started", table_name="task_execution")
    op.drop_table("task_execution")
    op.drop_table("task_execution_policy")
    op.drop_table("task_output_policy")
    op.drop_table("task_filter")
    op.drop_index("ix_task_source_downloader_id", table_name="task_source")
    op.drop_table("task_source")
    op.drop_index("ix_task_schedule_next_run_at", table_name="task_schedule")
    op.drop_table("task_schedule")
    op.drop_index("ix_task_definition_site_id", table_name="task_definition")
    op.drop_index("ix_task_definition_kind_status", table_name="task_definition")
    op.drop_table("task_definition")
