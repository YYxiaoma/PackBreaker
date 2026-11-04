"""Add per-site anti-abuse thresholds and per-execution risk events."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049_site_risk_control"
down_revision: str | None = "0048_unpack_delete_audit_v115"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "site",
        sa.Column("risk_control_limit", sa.Integer(), nullable=False, server_default=sa.text("3")),
    )
    op.add_column(
        "site",
        sa.Column(
            "risk_retry_interval_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60"),
        ),
    )
    op.add_column("site", sa.Column("risk_disabled_at", sa.DateTime(), nullable=True))
    op.create_table(
        "site_risk_event",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "site_id",
            sa.String(length=36),
            sa.ForeignKey("site.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "execution_id",
            sa.String(length=36),
            sa.ForeignKey("unpack_execution.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_id", sa.String(length=36), nullable=True),
        sa.Column("error_code", sa.String(length=96), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("threshold_reached", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_site_risk_event_execution_site_time",
        "site_risk_event",
        ["execution_id", "site_id", "occurred_at"],
    )
    op.create_index("ix_site_risk_event_occurred_at", "site_risk_event", ["occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_site_risk_event_occurred_at", table_name="site_risk_event")
    op.drop_index("ix_site_risk_event_execution_site_time", table_name="site_risk_event")
    op.drop_table("site_risk_event")
    op.drop_column("site", "risk_disabled_at")
    op.drop_column("site", "risk_retry_interval_seconds")
    op.drop_column("site", "risk_control_limit")
