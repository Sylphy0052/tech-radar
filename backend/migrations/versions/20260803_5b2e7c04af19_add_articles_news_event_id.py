"""add articles.news_event_id

Revision ID: 5b2e7c04af19
Revises: 3f0a5c9d21b4
Create Date: 2026-08-03 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "5b2e7c04af19"
down_revision: Union[str, Sequence[str], None] = "3f0a5c9d21b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 同一ニュースイベントのクラスタ ID（PROJECT_SPEC.md §17、Issue #20）。
    # duplicate_of_article_id が「どの代表記事の重複か」を表すのに対し、こちらは
    # 「どの出来事についての記事か」を表す。独自価値ありと判定されて別記事として
    # 残した記事も同じ ID を持つ。単独記事は NULL。
    op.add_column("articles", sa.Column("news_event_id", sa.Uuid(), nullable=True))
    op.create_index("ix_articles_news_event_id", "articles", ["news_event_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_articles_news_event_id", table_name="articles")
    op.drop_column("articles", "news_event_id")
