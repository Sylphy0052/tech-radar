"""add user_source_preferences

Revision ID: 3f0a5c9d21b4
Revises: 64e13e6f7f99
Create Date: 2026-08-03 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "3f0a5c9d21b4"
down_revision: Union[str, Sequence[str], None] = "64e13e6f7f99"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 情報源に対するユーザー固有の選好（PROJECT_SPEC.md §7.1 手順 4、Issue #34）。
    # 列構成は user_topic_preferences と揃える（対象がトピックか情報源かだけの違い）。
    op.create_table(
        "user_source_preferences",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source_domain", sa.Text(), nullable=False),
        sa.Column("positive_weight", sa.Float(), server_default="0", nullable=False),
        sa.Column("negative_weight", sa.Float(), server_default="0", nullable=False),
        sa.Column("effective_weight", sa.Float(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "source_domain", name="pk_user_source_preferences"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("user_source_preferences")
