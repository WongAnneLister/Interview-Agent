"""给保存岗位名称之前创建的面试补上岗位名（一次性，可重复运行）。

岗位名称原来只在创建面试时解析 JD 用一下就丢了；历史面试题按岗位分组需要它。
对 jd_title 为空的面试重新解析一次 JD，用实时评估那个便宜、不开思考的模型（LLM_LIVE_EVAL_*），
每场一次调用，用量照常记在 jd_parse。JD 里确实没写岗位名的保持为空。

用法：make backfill-job-titles（需要隧道已连上）
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.config import get_settings
from interview.db.models import Interview, UsageRole
from interview.db.session import create_engine, make_sessionmaker
from interview.domain.jd import parse_jd
from interview.domain.plan import InterviewRepository
from interview.prompts.structured import StructuredOutputError
from interview.providers.base import ProviderError
from interview.providers.observed import CallContext
from interview.providers.registry import ProviderRegistry


async def backfill(
    sessionmaker: async_sessionmaker[AsyncSession],
    providers: ProviderRegistry,
    *,
    user_id: uuid.UUID,
) -> tuple[int, int]:
    """返回（补上的场数，没能补上的场数）。"""
    async with sessionmaker() as session:
        pending = await InterviewRepository(session, user_id).list(Interview.jd_title.is_(None))
        work = [(row.id, row.jd_text) for row in pending]
    filled = failed = 0
    for interview_id, jd_text in work:
        ctx = CallContext(user_id=user_id, interview_id=interview_id, usage_role=UsageRole.JD_PARSE)
        try:
            title = (await parse_jd(providers.live_eval_llm(ctx), jd_text)).value.title.strip()
        except (ProviderError, StructuredOutputError) as exc:
            print(f"{interview_id} 解析失败：{type(exc).__name__}", file=sys.stderr)
            failed += 1
            continue
        if not title:
            failed += 1
            continue
        async with sessionmaker() as session:
            row = await InterviewRepository(session, user_id).get(interview_id)
            if row is not None and row.jd_title is None:
                row.jd_title = title[:200]
                await session.commit()
                filled += 1
    return filled, failed


async def run() -> int:
    settings = get_settings()
    if settings.app.default_user_id is None:
        print("DEFAULT_USER_ID 未配置：先运行 make seed", file=sys.stderr)
        return 1
    engine = create_engine(settings.db.url.get_secret_value(), db=settings.db)
    try:
        filled, failed = await backfill(
            make_sessionmaker(engine),
            ProviderRegistry(settings.providers),
            user_id=settings.app.default_user_id,
        )
    finally:
        await engine.dispose()
    print(f"补上岗位名 {filled} 场；没能补上 {failed} 场")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
