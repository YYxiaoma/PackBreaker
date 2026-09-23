"""为站点独立下载凭据预留加密引用；不开放候选站点或复制主凭据。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_site_download_credential_v101"
down_revision: str | None = "0029_v018_compatibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable with no default: all existing site rows retain their existing
    # credentials and remain unaffected by the new optional reference.
    op.add_column(
        "site",
        sa.Column("download_secret_id", sa.String(length=36), nullable=True),
    )


def downgrade() -> None:
    # A downgrade must never rebuild the SQLite site parent table while
    # dependent task records exist, or silently discard encrypted credentials.
    # Leaving this optional column intact is compatible with older code.
    pass
