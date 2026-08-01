"""add article duplicate columns

Revision ID: f3a9c1d4b8e6
Revises: 2686658bb2d2
Create Date: 2026-08-01 15:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f3a9c1d4b8e6"
down_revision: Union[str, Sequence[str], None] = "2686658bb2d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    代表記事への自己参照 (duplicate_of_article_id) と重複減点 (duplicate_penalty) を
    追加する。代表記事は duplicate_of_article_id IS NULL であり、クラスタは同じ
    代表 ID でグループ化する。
    """
    op.add_column(
        "articles",
        sa.Column("duplicate_of_article_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "articles",
        sa.Column("duplicate_penalty", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_foreign_key(
        op.f("fk_articles_duplicate_of_article_id_articles"),
        "articles",
        "articles",
        ["duplicate_of_article_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # クラスタ単位の取得と、代表記事だけを絞り込む用途で使う。
    op.create_index("ix_articles_duplicate_of_article_id", "articles", ["duplicate_of_article_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_articles_duplicate_of_article_id", table_name="articles")
    op.drop_constraint(
        op.f("fk_articles_duplicate_of_article_id_articles"), "articles", type_="foreignkey"
    )
    op.drop_column("articles", "duplicate_penalty")
    op.drop_column("articles", "duplicate_of_article_id")
