"""add recommendation_runs.filter_fingerprint

Revision ID: c65b5f9dd6fd
Revises: 5b2e7c04af19
Create Date: 2026-08-14 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c65b5f9dd6fd"
down_revision: Union[str, Sequence[str], None] = "5b2e7c04af19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 検索・絞り込み条件（recommendation.service.FeedFilters）を正規化してハッシュ化
    # した値（Issue #90）。GET /api/feed の run 再利用判定が user_id + mode に加えて
    # この列でも絞り込む。既存行は NULL のままでよい（次回生成時に新しい run が
    # 作られるだけで、データ移行は不要）。
    op.add_column("recommendation_runs", sa.Column("filter_fingerprint", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("recommendation_runs", "filter_fingerprint")
