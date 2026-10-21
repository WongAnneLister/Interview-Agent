"""Allow both report prompt versions to be recorded.

Revision ID: f7b3a9d2e6c1
Revises: e2a7c4d9f1b6
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7b3a9d2e6c1"
down_revision: str | Sequence[str] | None = "e2a7c4d9f1b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("reports", "prompt_version", existing_type=sa.String(32), type_=sa.String(64))


def downgrade() -> None:
    # 完整双版本超过旧字段容量，回滚时清空这类元数据，保留报告本身。
    op.execute("UPDATE reports SET prompt_version = NULL WHERE length(prompt_version) > 32")
    op.alter_column("reports", "prompt_version", existing_type=sa.String(64), type_=sa.String(32))
