"""Keep the job title parsed from the JD so past questions can be grouped by job.

Revision ID: b6d2f8a4c9e1
Revises: a3c8e1f5b7d2
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6d2f8a4c9e1"
down_revision: str | Sequence[str] | None = "a3c8e1f5b7d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 旧面试没有解析结果可用：留空，接口用 JD 正文第一行兜底
    op.add_column("interviews", sa.Column("jd_title", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("interviews", "jd_title")
