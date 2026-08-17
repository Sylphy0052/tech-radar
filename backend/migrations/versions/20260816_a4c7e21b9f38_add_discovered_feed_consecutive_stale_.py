"""add discovered feed consecutive stale fetches column

Revision ID: a4c7e21b9f38
Revises: c3b1f4231c4b
Create Date: 2026-08-16 17:10:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a4c7e21b9f38"
down_revision: Union[str, Sequence[str], None] = "c3b1f4231c4b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 新着が出ないフィードの無効化用（Issue #109）。エントリは配信したが絞り込み後に
    # 新着が残らなかった連続回数を追加する。
    op.add_column(
        "discovered_feeds",
        sa.Column("consecutive_stale_fetches", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("discovered_feeds", "consecutive_stale_fetches")
