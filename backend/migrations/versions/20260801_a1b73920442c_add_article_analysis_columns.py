"""add article analysis columns

Revision ID: a1b73920442c
Revises: e540cb265493
Create Date: 2026-08-01 13:36:19.431587

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b73920442c"
down_revision: Union[str, Sequence[str], None] = "e540cb265493"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 解析した時点の body_hash。同じ本文を二度 LLM へ渡さないために使う。
    op.add_column("articles", sa.Column("analyzed_body_hash", sa.String(length=64), nullable=True))
    # 解析の進行状態（pending / analyzing / completed / failed）。
    op.add_column("articles", sa.Column("analysis_status", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("articles", "analysis_status")
    op.drop_column("articles", "analyzed_body_hash")
