"""add_plan_terms_and_stages

Revision ID: c4d9e2f7a1b3
Revises: 8f3a1c2d9b47
Create Date: 2026-10-18 17:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4d9e2f7a1b3"
down_revision: str | Sequence[str] | None = "8f3a1c2d9b47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column(
            "tech_terms_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    op.add_column(
        "plans",
        sa.Column(
            "stages_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("plans", "stages_json")
    op.drop_column("plans", "tech_terms_json")
