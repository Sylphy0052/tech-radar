"""add job available at

Revision ID: 776773dd4fc3
Revises: f3a9c1d4b8e6
Create Date: 2026-08-01 18:01:09.316518

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "776773dd4fc3"
# 当初は 2686658bb2d2 を親としていたが、同じ親から生えた f3a9c1d4b8e6 (Issue #10) が
# 先にマージされ head が2つに分岐したため、後発のこちらを直列につなぎ直した。
# 両者は別テーブル (articles / jobs) を触るため順序を入れ替えても内容は衝突しない。
down_revision: Union[str, Sequence[str], None] = "f3a9c1d4b8e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # リトライの指数バックオフを DB 側に持たせる。既存行は即時実行可能とする。
    op.add_column(
        "jobs",
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # ワーカーの取得条件が created_at 順から available_at 順へ変わるため索引も差し替える。
    op.drop_index("ix_jobs_status_created_at", table_name="jobs")
    op.create_index("ix_jobs_status_available_at", "jobs", ["status", "available_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_jobs_status_available_at", table_name="jobs")
    op.create_index("ix_jobs_status_created_at", "jobs", ["status", "created_at"])
    op.drop_column("jobs", "available_at")
