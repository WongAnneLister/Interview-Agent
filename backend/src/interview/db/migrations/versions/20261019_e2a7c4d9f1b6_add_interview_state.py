"""add_interview_state

Revision ID: e2a7c4d9f1b6
Revises: c4d9e2f7a1b3
Create Date: 2026-10-19 20:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e2a7c4d9f1b6"
down_revision: str | Sequence[str] | None = "c4d9e2f7a1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "interviews",
        sa.Column("state_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "evaluations",
        sa.Column(
            "detail_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("evaluations", "detail_json")
    op.drop_column("interviews", "state_json")
