"""Remember resume file names and content hashes so saved resumes can be reused.

Revision ID: a3c8e1f5b7d2
Revises: f7b3a9d2e6c1
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3c8e1f5b7d2"
down_revision: str | Sequence[str] | None = "f7b3a9d2e6c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 旧简历没有这两项：照常出现在列表里，只是同一文件再上传时会再解析一次
    op.add_column("resumes", sa.Column("file_name", sa.String(255), nullable=True))
    op.add_column("resumes", sa.Column("file_sha256", sa.String(64), nullable=True))
    op.create_index("ix_resumes_user_id_file_sha256", "resumes", ["user_id", "file_sha256"])


def downgrade() -> None:
    op.drop_index("ix_resumes_user_id_file_sha256", table_name="resumes")
    op.drop_column("resumes", "file_sha256")
    op.drop_column("resumes", "file_name")
