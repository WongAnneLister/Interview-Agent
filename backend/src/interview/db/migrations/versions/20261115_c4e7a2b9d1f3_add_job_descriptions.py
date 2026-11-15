"""Save parsed job descriptions so the same JD is not parsed again.

Revision ID: c4e7a2b9d1f3
Revises: b6d2f8a4c9e1
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4e7a2b9d1f3"
down_revision: str | Sequence[str] | None = "b6d2f8a4c9e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_descriptions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("parsed_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=False),
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
            ["user_id"],
            ["users.id"],
            name=op.f("fk_job_descriptions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_descriptions")),
        sa.UniqueConstraint(
            "user_id", "text_sha256", name=op.f("uq_job_descriptions_user_id_text_sha256")
        ),
    )
    op.create_index(
        "ix_job_descriptions_user_id_updated_at", "job_descriptions", ["user_id", "updated_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_job_descriptions_user_id_updated_at", table_name="job_descriptions")
    op.drop_table("job_descriptions")
