"""已解析的职位描述：同一用户再贴同一份 JD 时直接复用解析结果，不再调用模型（C47）。

- 同一份：正文去掉首尾空白、合并连续空白后算 SHA-256；多一个空格或换行方式不同也算同一份。
- 解析结果连同 jd_parse 提示词版本一起存；提示词升级后旧结果作废，下次使用时重新解析并覆盖。
- 贴 JD 时的预览（/api/jd/parse）和创建面试都走这里，一份 JD 最多解析一次。
- 写入时与并发的同一份 JD 撞上唯一约束：说明别人刚存好，丢弃自己这份即可。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any, cast

import structlog
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.db.models import JobDescription
from interview.db.repository import UserScopedRepository
from interview.domain.jd import JDParsed, parse_jd
from interview.prompts.loader import get_prompt_library
from interview.providers.base import LLMProvider

log = structlog.get_logger(__name__)

_SPACES = re.compile(r"\s+")


class JobDescriptionRepository(UserScopedRepository[JobDescription]):
    model = JobDescription


@dataclass(frozen=True)
class SavedJD:
    id: uuid.UUID
    parsed: JDParsed
    reused: bool  # True：直接用了之前的解析结果，没有调用模型


def jd_digest(text: str) -> str:
    return hashlib.sha256(_SPACES.sub(" ", text.strip()).encode("utf-8")).hexdigest()


def current_version() -> int:
    return get_prompt_library().get("jd_parse").meta.version


async def parsed_jd(
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: LLMProvider,
    *,
    user_id: uuid.UUID,
    text: str,
    interview_id: uuid.UUID | None = None,
) -> SavedJD:
    """返回这份 JD 的解析结果：存过且提示词版本没变就复用，否则解析一次并存下来。

    结构化输出失败时抛 StructuredOutputError，由调用方处理。
    """
    digest = jd_digest(text)
    version = current_version()
    async with sessionmaker() as session:
        row = await _find(session, user_id, digest)
        if row is not None and row.prompt_version == version:
            row.text = text
            row.updated_at = cast(Any, func.now())  # 最近用过的排在列表前面
            await session.commit()
            log.info("jd_reused", jd_id=str(row.id))
            return SavedJD(row.id, JDParsed.model_validate(row.parsed_json), reused=True)

    parsed = (await parse_jd(llm, text, interview_id=interview_id)).value
    async with sessionmaker() as session:
        row = await _find(session, user_id, digest)
        if row is None:
            row = JobDescription(user_id=user_id, text_sha256=digest)
            JobDescriptionRepository(session, user_id).add(row)
        row.text = text
        row.title = parsed.title[:200] or None
        row.parsed_json = parsed.model_dump(mode="json")
        row.prompt_version = version
        try:
            await session.commit()
        except IntegrityError:
            # 同一份 JD 并发解析，对方先存好了：用自己这份结果即可，不必再写
            await session.rollback()
            row = await _find(session, user_id, digest)
            if row is None:
                raise
        return SavedJD(row.id, parsed, reused=False)


async def _find(session: AsyncSession, user_id: uuid.UUID, digest: str) -> JobDescription | None:
    repo = JobDescriptionRepository(session, user_id)
    result = await session.execute(repo.select().where(JobDescription.text_sha256 == digest))
    return result.scalar_one_or_none()
