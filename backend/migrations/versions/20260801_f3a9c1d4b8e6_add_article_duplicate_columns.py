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

    あわせて独自価値判定 (LLM) 結果のキャッシュ列を追加する。
    unique_value_judged_body_hash は判定時点の body_hash、has_unique_value は
    直近の判定結果。本文が変わっていなければ判定を使い回し、同じ記事へ
    再実行のたびに LLM を呼ばないようにする（analyzed_body_hash /
    embedding_body_hash と同じ役割）。
    """
    op.add_column(
        "articles",
        sa.Column("duplicate_of_article_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "articles",
        sa.Column("duplicate_penalty", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "articles",
        sa.Column("unique_value_judged_body_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "articles",
        sa.Column("has_unique_value", sa.Boolean(), nullable=False, server_default="false"),
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
    op.drop_column("articles", "has_unique_value")
    op.drop_column("articles", "unique_value_judged_body_hash")
    op.drop_column("articles", "duplicate_penalty")
    op.drop_column("articles", "duplicate_of_article_id")
