"""allow site-only seeding watch records

Revision ID: 0059_seeding_site_only_watch
Revises: 0058_seeding_health_runtime_settings
Create Date: 2026-11-20

A torrent that exists on the site but not in any downloader has no downloader
record. Represent it as a SITE_ONLY watch so the existing issue table, detail
drawer and state machine can be reused without a second issue pipeline.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0059_seeding_site_only_watch"
down_revision: str | None = "0058_seeding_health_runtime_settings"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_PRESENCE = "presence IN ('ACTIVE', 'GONE', 'SITE_ONLY')"


def upgrade() -> None:
    op.execute(
        "DELETE FROM seeding_health_issue WHERE watch_id IN "
        "(SELECT id FROM seeding_watch WHERE presence = 'SITE_ONLY')"
    )
    op.execute("DELETE FROM seeding_watch WHERE presence = 'SITE_ONLY'")
    with op.batch_alter_table("seeding_watch", recreate="always") as batch:
        batch.drop_constraint("ck_seeding_watch_presence", type_="check")
        batch.create_check_constraint("ck_seeding_watch_presence", _PRESENCE)
        batch.alter_column("downloader_id", existing_type=sa.String(length=36), nullable=True)


def downgrade() -> None:
    op.execute(
        "DELETE FROM seeding_health_issue WHERE watch_id IN "
        "(SELECT id FROM seeding_watch WHERE presence = 'SITE_ONLY')"
    )
    op.execute("DELETE FROM seeding_watch WHERE presence = 'SITE_ONLY'")
    with op.batch_alter_table("seeding_watch", recreate="always") as batch:
        batch.drop_constraint("ck_seeding_watch_presence", type_="check")
        batch.create_check_constraint("ck_seeding_watch_presence", "presence IN ('ACTIVE', 'GONE')")
        batch.alter_column("downloader_id", existing_type=sa.String(length=36), nullable=False)
