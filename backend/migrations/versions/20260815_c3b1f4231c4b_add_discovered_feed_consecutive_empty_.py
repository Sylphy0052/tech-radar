"""add discovered feed consecutive empty fetches column

Revision ID: c3b1f4231c4b
Revises: 92d5bbff583d
Create Date: 2026-08-15 22:46:31.860772

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3b1f4231c4b"
down_revision: Union[str, Sequence[str], None] = "92d5bbff583d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 記事を配信しないフィードの無効化用（Issue #108）。連続でエントリ0件だった回数を追加する。
    op.add_column(
        "discovered_feeds",
        sa.Column("consecutive_empty_fetches", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("discovered_feeds", "consecutive_empty_fetches")
