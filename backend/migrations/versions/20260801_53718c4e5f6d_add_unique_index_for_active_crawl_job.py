"""add unique index for active crawl job

Revision ID: 53718c4e5f6d
Revises: 776773dd4fc3
Create Date: 2026-08-01 20:59:27.616972

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "53718c4e5f6d"
down_revision: Union[str, Sequence[str], None] = "776773dd4fc3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACTIVE_CRAWL_JOB_INDEX = "ux_jobs_active_crawl_sources"

# crawl_sources が取りうる実行中 status は searching だけ（`jobs/status.py` の
# ジョブ種別 -> 実行中 status の写像）。pending と合わせた2つが「まだ終わっていない」
# 状態にあたる。
ACTIVE_CRAWL_JOB_PREDICATE = "type = 'crawl_sources' AND status IN ('pending', 'searching')"


def upgrade() -> None:
    """Upgrade schema."""
    # 巡回ジョブの重複起動を DB 側で1件に制限する（Issue #26）。
    #
    # API 側は enqueue 前にアクティブなジョブの有無を SELECT で確認しているが、
    # 確認と INSERT の間に別リクエストが割り込む TOCTOU レースを、アプリの
    # コードだけでは塞げない（PostgreSQL には存在しない行に対するギャップロックが
    # ないため、SELECT ... FOR UPDATE でも「まだ1件も無い」状態は守れない）。
    # 一意制約を最終的な防衛線として置き、API 側は違反を捕捉して既存ジョブを返す。
    op.create_index(
        ACTIVE_CRAWL_JOB_INDEX,
        "jobs",
        ["type"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_CRAWL_JOB_PREDICATE),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(ACTIVE_CRAWL_JOB_INDEX, table_name="jobs")
