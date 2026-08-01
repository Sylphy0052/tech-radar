"""widen source registry unique constraint

Revision ID: 2686658bb2d2
Revises: a1b73920442c
Create Date: 2026-08-01 14:47:07.706113

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2686658bb2d2"
down_revision: Union[str, Sequence[str], None] = "a1b73920442c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRAINT_NAME = "uq_source_registry_domain"


def upgrade() -> None:
    """Upgrade schema.

    一意制約に github_org を加える。github.com の Release 規則は org 単位で
    別物であり、(domain, path_pattern) だけでは 1 組織分しか登録できない。
    """
    op.drop_constraint(CONSTRAINT_NAME, "source_registry", type_="unique")
    op.create_unique_constraint(
        CONSTRAINT_NAME,
        "source_registry",
        ["domain", "path_pattern", "github_org"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(CONSTRAINT_NAME, "source_registry", type_="unique")
    op.create_unique_constraint(
        CONSTRAINT_NAME,
        "source_registry",
        ["domain", "path_pattern"],
        postgresql_nulls_not_distinct=True,
    )
