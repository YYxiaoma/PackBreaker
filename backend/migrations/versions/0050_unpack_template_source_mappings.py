"""Persist source-to-torrent mapping hints in template import jobs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050_unpack_template_source_mappings"
down_revision: str | None = "0049_unpack_template_library"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "unpack_template_import_job",
        sa.Column(
            "seed_mappings",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("unpack_template_import_job", "seed_mappings")
