"""v1.0.15 保存前目录扫描临时持久化。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_unpack_source_scans"
down_revision: str | None = "0035_unpack_v2_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "unpack_source_scan",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("directory_path", sa.Text(), nullable=False),
        sa.Column("file_filter", sa.JSON(), nullable=False),
        sa.Column("discovered_count", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_unpack_source_scan_version_positive"),
        ),
        sa.CheckConstraint(
            "discovered_count >= 0",
            name=op.f("ck_unpack_source_scan_discovered_count"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unpack_source_scan")),
    )
    op.create_index(
        "ix_unpack_source_scan_expires",
        "unpack_source_scan",
        ["expires_at"],
    )

    op.create_table(
        "unpack_source_scan_item",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scan_id", sa.String(length=36), nullable=False),
        sa.Column("source_object_key", sa.String(length=64), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("canonical_path_hint", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("extension", sa.String(length=32), nullable=False),
        sa.Column("resolution", sa.String(length=16), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("device", sa.BigInteger(), nullable=False),
        sa.Column("inode", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.String(length=32), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name=op.f("ck_unpack_source_scan_item_size_bytes"),
        ),
        sa.ForeignKeyConstraint(
            ["scan_id"],
            ["unpack_source_scan.id"],
            name=op.f("fk_unpack_source_scan_item_scan_id_unpack_source_scan"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unpack_source_scan_item")),
        sa.UniqueConstraint(
            "scan_id",
            "source_object_key",
            name="uq_unpack_source_scan_item_key",
        ),
        sa.UniqueConstraint(
            "scan_id",
            "relative_path",
            name="uq_unpack_source_scan_item_path",
        ),
    )
    op.create_index(
        "ix_unpack_source_scan_item_scan_selected",
        "unpack_source_scan_item",
        ["scan_id", "selected", "id"],
    )
    op.create_index(
        "ix_unpack_source_scan_item_scan_extension",
        "unpack_source_scan_item",
        ["scan_id", "extension", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_unpack_source_scan_item_scan_extension",
        table_name="unpack_source_scan_item",
    )
    op.drop_index(
        "ix_unpack_source_scan_item_scan_selected",
        table_name="unpack_source_scan_item",
    )
    op.drop_table("unpack_source_scan_item")
    op.drop_index("ix_unpack_source_scan_expires", table_name="unpack_source_scan")
    op.drop_table("unpack_source_scan")
