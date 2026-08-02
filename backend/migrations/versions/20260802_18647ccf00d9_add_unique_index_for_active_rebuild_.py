"""add unique index for active rebuild_interest_clusters job per user

Revision ID: 18647ccf00d9
Revises: c7f21d0be4a3
Create Date: 2026-08-02 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "18647ccf00d9"
down_revision: Union[str, Sequence[str], None] = "c7f21d0be4a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACTIVE_REBUILD_INTEREST_CLUSTERS_JOB_INDEX = "ux_jobs_active_rebuild_interest_clusters"

# rebuild_interest_clusters が取りうる実行中 status は searching だけ
# （`jobs/status.py` のジョブ種別 -> 実行中 status の写像）。pending と合わせた
# 2つが「まだ終わっていない」状態にあたる。`ux_jobs_active_crawl_sources`
# （Issue #26）と同じ設計だが、こちらはユーザーごとに1件までに絞る必要があるため
# （複数ユーザーで同時に走ってよい）、payload の user_id も式インデックスに含める。
ACTIVE_REBUILD_INTEREST_CLUSTERS_JOB_PREDICATE = (
    "type = 'rebuild_interest_clusters' AND status IN ('pending', 'searching')"
)


def upgrade() -> None:
    """Upgrade schema.

    Good/保存/Bad フィードバックのたびに `rebuild_interest_clusters` ジョブを
    積むと、連打された分だけ全関心記事の embedding クラスタリングが積み上がり
    無駄になる（Issue #15 段階 3）。`api/feedback.py` は enqueue 前に同じ user の
    pending ジョブの有無を SELECT で確認するが、確認と INSERT の間に別リクエスト
    が割り込む TOCTOU レースをアプリ側だけでは塞げない
    （`ux_jobs_active_crawl_sources` の upgrade() コメントと同じ理由）。
    一意制約を最終的な防衛線として置き、API 側は違反を捕捉して何もしない
    （既に同じ user の再構築ジョブが積まれているとみなせるため）。
    """
    op.create_index(
        ACTIVE_REBUILD_INTEREST_CLUSTERS_JOB_INDEX,
        "jobs",
        [sa.text("(payload->>'user_id')")],
        unique=True,
        postgresql_where=sa.text(ACTIVE_REBUILD_INTEREST_CLUSTERS_JOB_PREDICATE),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(ACTIVE_REBUILD_INTEREST_CLUSTERS_JOB_INDEX, table_name="jobs")
