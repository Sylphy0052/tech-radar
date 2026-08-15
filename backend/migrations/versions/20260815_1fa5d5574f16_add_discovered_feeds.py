"""add discovered_feeds

Revision ID: 1fa5d5574f16
Revises: c65b5f9dd6fd
Create Date: 2026-08-15 12:27:15.404736

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "1fa5d5574f16"
down_revision: Union[str, Sequence[str], None] = "c65b5f9dd6fd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 登録記事のドメイン集計から自動発見した巡回対象（Issue #93）。
    op.create_table(
        "discovered_feeds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("feed_url", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("article_count", sa.Integer(), nullable=False),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="false", nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovered_feeds")),
        sa.UniqueConstraint("domain", name=op.f("uq_discovered_feeds_domain")),
    )
    op.create_index(
        "ix_discovered_feeds_status_enabled",
        "discovered_feeds",
        ["status", "enabled"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_discovered_feeds_status_enabled", table_name="discovered_feeds")
    op.drop_table("discovered_feeds")
