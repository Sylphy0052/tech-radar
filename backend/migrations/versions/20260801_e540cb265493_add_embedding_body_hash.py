"""add embedding body hash

Revision ID: e540cb265493
Revises: 28118191000a
Create Date: 2026-08-01 13:10:48.513877

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e540cb265493"
down_revision: Union[str, Sequence[str], None] = "28118191000a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # embedding を生成した時点の body_hash。本文が更新されたら作り直す判定に使う。
    op.add_column("articles", sa.Column("embedding_body_hash", sa.String(length=64), nullable=True))
    # 既存行は本文更新の有無を判定できないため、次回アクセス時に作り直させる。
    op.execute("UPDATE articles SET embedding = NULL WHERE embedding IS NOT NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("articles", "embedding_body_hash")
