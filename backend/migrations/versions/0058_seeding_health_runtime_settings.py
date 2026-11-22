"""add seeding health runtime settings

Revision ID: 0058_seeding_health_runtime_settings
Revises: 0057_seeding_health_monitor
Create Date: 2026-11-20

Runtime toggle for the site-side probe. No default row is inserted: until the
user changes it explicitly, the environment-variable default still applies.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058_seeding_health_runtime_settings"
down_revision: str | None = "0057_seeding_health_monitor"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "seeding_health_config",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("site_probe_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_seeding_health_config"),
    )


def downgrade() -> None:
    op.drop_table("seeding_health_config")
