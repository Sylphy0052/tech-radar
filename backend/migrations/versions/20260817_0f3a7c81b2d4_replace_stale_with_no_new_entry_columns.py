"""replace stale fetch column with no new entry columns

Revision ID: 0f3a7c81b2d4
Revises: a4c7e21b9f38
Create Date: 2026-08-17 09:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0f3a7c81b2d4"
down_revision: Union[str, Sequence[str], None] = "a4c7e21b9f38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Issue #111。a4c7e21b9f38 が追加した `consecutive_stale_fetches` を落とし、
    # 実際に採用した判定 (ADR 0008) の列へ入れ替える。
    #
    # `consecutive_stale_fetches` は #109 のマージで列と関数だけが main へ入ったが、
    # 巡回パイプラインから呼ぶ配線が一緒に入らなかったため、書き込むコードが
    # 一度も存在しない。全行 0 のままであり、落としても失われる情報は無い。
    op.drop_column("discovered_feeds", "consecutive_stale_fetches")

    # 除外を通り抜けた候補が 0 件だった連続回数と、直近で新着を出した時刻。
    # 前者で無効化を判定し、後者は診断用に記録するだけで判定には使わない
    # (巡回が手動起動のため、経過時間では「新着が無い」と「巡回していない」を
    # 区別できない。ADR 0008)。
    op.add_column(
        "discovered_feeds",
        sa.Column("consecutive_no_new_entries", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "discovered_feeds",
        sa.Column("last_new_entry_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("discovered_feeds", "last_new_entry_at")
    op.drop_column("discovered_feeds", "consecutive_no_new_entries")
    op.add_column(
        "discovered_feeds",
        sa.Column("consecutive_stale_fetches", sa.Integer(), server_default="0", nullable=False),
    )
