"""Add reusable unpack template metadata and template task definitions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


def _set_sqlite_foreign_keys(enabled: bool) -> None:
    """Keep child rows intact while SQLite rebuilds the referenced task table."""

    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return
    expected = 1 if enabled else 0
    pragma = "ON" if enabled else "OFF"
    # SQLite cannot toggle foreign_keys inside a transaction. Alembic's autocommit
    # block safely brackets this connection-level setting and commits each side.
    with op.get_context().autocommit_block():
        op.get_bind().exec_driver_sql(f"PRAGMA foreign_keys={pragma}")
        actual = int(op.get_bind().exec_driver_sql("PRAGMA foreign_keys").scalar_one())
        if actual != expected:
            raise RuntimeError(f"Unable to set SQLite foreign_keys={pragma}")


revision: str = "0049_unpack_template_library"
down_revision: str | None = "0049_site_risk_control"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _set_sqlite_foreign_keys(False)
    op.create_table(
        "unpack_template",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("source_provenance", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name", name="name"),
    )
    op.create_index("ix_unpack_template_updated", "unpack_template", ["updated_at"])
    op.create_table(
        "unpack_template_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "template_id",
            sa.String(36),
            sa.ForeignKey("unpack_template.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("expected_seed_count", sa.Integer(), nullable=False),
        sa.Column("seed_count", sa.Integer(), nullable=False),
        sa.Column("site_keys", sa.JSON(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("source_provenance", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("template_id", "version", name="template_version"),
        sa.CheckConstraint("version >= 1", name="ck_unpack_template_version_version_positive"),
        sa.CheckConstraint(
            "expected_seed_count >= 0", name="ck_unpack_template_version_expected_seed_count"
        ),
        sa.CheckConstraint("seed_count >= 0", name="ck_unpack_template_version_seed_count"),
        sa.CheckConstraint(
            "status IN ('IMPORTING', 'READY', 'INCOMPLETE')",
            name="ck_unpack_template_version_status",
        ),
    )
    op.create_index(
        "ix_unpack_template_version_status", "unpack_template_version", ["template_id", "status"]
    )
    op.create_table(
        "unpack_torrent_metadata",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("site_key", sa.String(64), nullable=False),
        sa.Column("remote_torrent_id", sa.String(128), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("descriptor", sa.JSON(), nullable=False),
        sa.Column("total_size", sa.BigInteger(), nullable=True),
        sa.Column("file_summary", sa.JSON(), nullable=False),
        sa.Column("external_ids", sa.JSON(), nullable=False),
        sa.Column("info_hash", sa.String(128), nullable=True),
        sa.Column("metadata_digest", sa.String(64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("verification_status", sa.String(24), nullable=False),
        sa.Column("source_provenance", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("site_key", "remote_torrent_id", name="site_remote_id"),
        sa.CheckConstraint(
            "verification_status IN ('DETAILS_VERIFIED', 'STALE', 'INVALID')",
            name="ck_unpack_torrent_metadata_verification_status",
        ),
    )
    op.create_index(
        "ix_unpack_torrent_metadata_digest", "unpack_torrent_metadata", ["metadata_digest"]
    )
    op.create_table(
        "unpack_template_seed",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "template_version_id",
            sa.String(36),
            sa.ForeignKey("unpack_template_version.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "metadata_id",
            sa.String(36),
            sa.ForeignKey("unpack_torrent_metadata.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("mapping_rules", sa.JSON(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("template_version_id", "metadata_id", name="template_metadata"),
    )
    op.create_index(
        "ix_unpack_template_seed_version_order",
        "unpack_template_seed",
        ["template_version_id", "sort_order"],
    )
    op.create_table(
        "unpack_template_import_job",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "template_version_id",
            sa.String(36),
            sa.ForeignKey("unpack_template_version.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "site_config_id",
            sa.String(36),
            sa.ForeignKey("site.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("site_key", sa.String(64), nullable=False),
        sa.Column("torrent_ids", sa.JSON(), nullable=False),
        sa.Column("next_index", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS')",
            name="ck_unpack_template_import_job_status",
        ),
        sa.CheckConstraint("next_index >= 0", name="ck_unpack_template_import_job_next_index"),
        sa.CheckConstraint(
            "success_count >= 0", name="ck_unpack_template_import_job_success_count"
        ),
        sa.CheckConstraint(
            "failure_count >= 0", name="ck_unpack_template_import_job_failure_count"
        ),
    )
    op.create_index(
        "ix_unpack_template_import_job_status_created",
        "unpack_template_import_job",
        ["status", "created_at"],
    )

    with op.batch_alter_table("unpack_definition") as batch_op:
        batch_op.add_column(
            sa.Column("task_kind", sa.String(16), nullable=False, server_default="MANUAL")
        )
        batch_op.add_column(sa.Column("template_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("template_version", sa.Integer(), nullable=True))
        batch_op.drop_constraint(op.f("ck_unpack_definition_manual_directory_only"), type_="check")

    # Existing monitor definitions must be tagged before the consistency check is added.
    op.execute("UPDATE unpack_definition SET task_kind = 'MONITOR' WHERE trigger_kind = 'MONITOR'")

    with op.batch_alter_table("unpack_definition") as batch_op:
        batch_op.create_check_constraint(
            "task_kind", "task_kind IN ('MANUAL', 'MONITOR', 'TEMPLATE')"
        )
        batch_op.create_check_constraint(
            "task_trigger_consistency",
            "(task_kind = 'MONITOR' AND trigger_kind = 'MONITOR') OR "
            "(task_kind IN ('MANUAL', 'TEMPLATE') AND trigger_kind = 'MANUAL')",
        )
        batch_op.create_check_constraint(
            "template_reference",
            "(task_kind = 'TEMPLATE' AND template_id IS NOT NULL "
            "AND template_version IS NOT NULL) OR "
            "(task_kind != 'TEMPLATE' AND template_id IS NULL AND template_version IS NULL)",
        )
        batch_op.create_check_constraint(
            "manual_directory_only",
            "(task_kind = 'TEMPLATE') OR (trigger_kind = 'MONITOR') OR source_kind = 'DIRECTORY'",
        )
        batch_op.create_foreign_key(
            "fk_unpack_definition_template_id_unpack_template",
            "unpack_template",
            ["template_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    _set_sqlite_foreign_keys(True)


def downgrade() -> None:
    _set_sqlite_foreign_keys(False)
    with op.batch_alter_table("unpack_definition") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_unpack_definition_template_id_unpack_template"), type_="foreignkey"
        )
        batch_op.drop_constraint(op.f("ck_unpack_definition_manual_directory_only"), type_="check")
        batch_op.drop_constraint(op.f("ck_unpack_definition_template_reference"), type_="check")
        batch_op.drop_constraint(
            op.f("ck_unpack_definition_task_trigger_consistency"), type_="check"
        )
        batch_op.drop_constraint(op.f("ck_unpack_definition_task_kind"), type_="check")
        batch_op.drop_column("template_version")
        batch_op.drop_column("template_id")
        batch_op.drop_column("task_kind")
        batch_op.create_check_constraint(
            "manual_directory_only",
            "(trigger_kind = 'MONITOR') OR source_kind = 'DIRECTORY'",
        )
    op.drop_index(
        "ix_unpack_template_import_job_status_created", table_name="unpack_template_import_job"
    )
    op.drop_table("unpack_template_import_job")
    op.drop_index("ix_unpack_template_seed_version_order", table_name="unpack_template_seed")
    op.drop_table("unpack_template_seed")
    op.drop_index("ix_unpack_torrent_metadata_digest", table_name="unpack_torrent_metadata")
    op.drop_table("unpack_torrent_metadata")
    op.drop_index("ix_unpack_template_version_status", table_name="unpack_template_version")
    op.drop_table("unpack_template_version")
    op.drop_index("ix_unpack_template_updated", table_name="unpack_template")
    op.drop_table("unpack_template")
    _set_sqlite_foreign_keys(True)
