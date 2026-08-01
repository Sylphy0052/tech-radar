"""add article registrations

Revision ID: 8964b9951fb0
Revises: 53718c4e5f6d
Create Date: 2026-08-01 20:56:58.409202

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "8964b9951fb0"
down_revision: Union[str, Sequence[str], None] = "53718c4e5f6d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ユーザーによる URL 登録の状態（`PROJECT_SPEC.md` §6.2）。取得前後で確定しない
    # 列を articles に持たせるとゴミ行が残るため、登録はここで別テーブルに分離する。
    op.create_table(
        "article_registrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("article_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("error_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_article_registrations_article_id_articles"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_article_registrations_job_id_jobs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_article_registrations")),
        sa.UniqueConstraint(
            "user_id",
            "normalized_url",
            name="uq_article_registrations_user_id_normalized_url",
        ),
    )
    op.create_index(
        "ix_article_registrations_user_id",
        "article_registrations",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_article_registrations_user_id", table_name="article_registrations")
    op.drop_table("article_registrations")
