"""add gin index for article topics

Revision ID: 64e13e6f7f99
Revises: 18647ccf00d9
Create Date: 2026-08-02 19:12:30.893761

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "64e13e6f7f99"
down_revision: Union[str, Sequence[str], None] = "18647ccf00d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # トピック単位の選好更新（`interest.service._load_recent_topic_actions`）が
    # `Article.topics.contains([topic])`（JSONB `@>`）で記事を絞り込む。フィード
    # バックのたびに記事のトピック数だけ実行されるホットパスのため、インデックス
    # 無しでは記事が増えるほど全件スキャンのコストが線形に増える（Issue #15
    # 自己レビュー 4）。`@>` の containment 演算子だけを使い、範囲検索や存在演算子
    # （`?`）は使わないため、汎用の jsonb_ops より軽量な jsonb_path_ops を選ぶ。
    op.create_index(
        "ix_articles_topics_gin",
        "articles",
        ["topics"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"topics": "jsonb_path_ops"},
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_articles_topics_gin", table_name="articles")
