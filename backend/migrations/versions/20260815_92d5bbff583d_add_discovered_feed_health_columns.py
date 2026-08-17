"""add discovered feed health columns

Revision ID: 92d5bbff583d
Revises: 1fa5d5574f16
Create Date: 2026-08-15 21:57:37.136805

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "92d5bbff583d"
down_revision: Union[str, Sequence[str], None] = "1fa5d5574f16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 発見済みフィードの死活監視用（Issue #105）。連続失敗回数と直近成功時刻を追加する。
    op.add_column(
        "discovered_feeds",
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "discovered_feeds",
        sa.Column("last_succeeded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("discovered_feeds", "last_succeeded_at")
    op.drop_column("discovered_feeds", "consecutive_failures")
