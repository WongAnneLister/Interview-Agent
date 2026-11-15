"""给加指纹之前上传的简历补上原件的 SHA-256（一次性，可重复运行）。

补上之后，简历列表里同一文件的多份只显示一份，再上传同一文件也会直接复用。
原件在对象存储里找不到的简历保持原样，照常出现在列表里。

用法：make backfill-resume-hashes（需要隧道已连上）
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.config import get_settings
from interview.db.models import Resume
from interview.db.session import create_engine, make_sessionmaker
from interview.domain.resume import ResumeRepository
from interview.storage.base import ObjectStore, StorageError
from interview.storage.s3 import S3ObjectStore


async def backfill(
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    bucket: str,
    user_id: uuid.UUID,
) -> tuple[int, int]:
    """返回（补上的份数，原件缺失的份数）。"""
    filled = missing = 0
    async with sessionmaker() as session:
        repo = ResumeRepository(session, user_id)
        for resume in await repo.list(Resume.file_sha256.is_(None)):
            try:
                data = await store.get(bucket, resume.file_key)
            except StorageError:
                missing += 1
                continue
            resume.file_sha256 = hashlib.sha256(data).hexdigest()
            filled += 1
        await session.commit()
    return filled, missing


async def run() -> int:
    settings = get_settings()
    if settings.app.default_user_id is None:
        print("DEFAULT_USER_ID 未配置：先运行 make seed", file=sys.stderr)
        return 1
    engine = create_engine(settings.db.url.get_secret_value(), db=settings.db)
    try:
        filled, missing = await backfill(
            make_sessionmaker(engine),
            S3ObjectStore(settings.s3),
            bucket=settings.s3.bucket_resumes,
            user_id=settings.app.default_user_id,
        )
    finally:
        await engine.dispose()
    print(f"补上指纹 {filled} 份；原件缺失 {missing} 份")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
