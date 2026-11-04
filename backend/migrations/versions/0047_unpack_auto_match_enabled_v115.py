"""拆包任务支持独立开启/关闭自动匹配。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_unpack_auto_match_enabled_v115"
down_revision: str | None = "0046_site_user_profile_v114"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "unpack_definition",
        sa.Column("auto_match_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("unpack_definition", "auto_match_enabled")
