"""add_usage_events

Revision ID: 8f3a1c2d9b47
Revises: 324e57bc1067
Create Date: 2026-10-14 23:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f3a1c2d9b47"
down_revision: str | Sequence[str] | None = "324e57bc1067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("interview_id", sa.Uuid(), nullable=True),
        sa.Column("turn_id", sa.Uuid(), nullable=True),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.Column(
            "role",
            sa.Enum(
                "resume_parse",
                "jd_parse",
                "plan",
                "front",
                "eval",
                "report",
                "asr",
                "tts",
                "eval_harness",
                name="usage_role",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column(
            "unit",
            sa.Enum(
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "reasoning_tokens",
                "audio_seconds",
                "characters",
                name="usage_unit",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("quantity", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("est_cost_cny", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("is_eval", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_retry", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_fallback", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("quantity >= 0", name=op.f("ck_usage_events_quantity_non_negative")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_usage_events_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_events")),
    )
    op.create_index(
        op.f("ix_usage_events_interview_id"), "usage_events", ["interview_id"], unique=False
    )
    op.create_index(op.f("ix_usage_events_turn_id"), "usage_events", ["turn_id"], unique=False)
    op.create_index(
        "ix_usage_events_user_id_created_at",
        "usage_events",
        ["user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_usage_events_user_id_created_at", table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_turn_id"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_interview_id"), table_name="usage_events")
    op.drop_table("usage_events")
