"""add recommendation_runs indexes

Revision ID: c7f21d0be4a3
Revises: 8964b9951fb0
Create Date: 2026-08-02 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c7f21d0be4a3"
down_revision: Union[str, Sequence[str], None] = "8964b9951fb0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 直近 run の再利用判定は user_id + mode で絞って generated_at 降順の先頭を取る
    # （`recommendation.service.build_latest_run_select`）。ソート順まで含めた
    # 複合インデックスにして、絞り込みと ORDER BY の両方をこの1本で賄う（Issue #32）。
    op.create_index(
        "ix_recommendation_runs_user_id_mode_generated_at",
        "recommendation_runs",
        ["user_id", "mode", sa.text("generated_at DESC"), sa.text("id DESC")],
        unique=False,
    )
    # 保持期間ジョブ（`jobs.handlers.purge_recommendation_runs`）の DELETE は
    # user_id で絞らず generated_at だけで範囲を切るため、単独のインデックスが要る。
    op.create_index(
        "ix_recommendation_runs_generated_at",
        "recommendation_runs",
        ["generated_at"],
        unique=False,
    )
    # user_id 単独のインデックスは上の複合インデックスの前方一致で代替できる。
    # 書き込みコストと容量の冗長を残さないため削除する。
    op.drop_index("ix_recommendation_runs_user_id", table_name="recommendation_runs")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_index(
        "ix_recommendation_runs_user_id",
        "recommendation_runs",
        ["user_id"],
        unique=False,
    )
    op.drop_index("ix_recommendation_runs_generated_at", table_name="recommendation_runs")
    op.drop_index(
        "ix_recommendation_runs_user_id_mode_generated_at",
        table_name="recommendation_runs",
    )
