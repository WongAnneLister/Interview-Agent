"""职位描述：贴 JD 时的解析预览，以及解析过的 JD 列表（C47）。

同一份 JD（忽略空白差异）只解析一次，之后预览、创建面试都直接复用结果。只读列表按 user_id 限定。
"""

import uuid
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from interview.api.deps import CurrentUserId, DbSession, get_jd_llm, get_resources
from interview.api.errors import AppError
from interview.api.resources import Resources
from interview.db.models import JobDescription
from interview.domain.jd import JDParsed, JDText
from interview.domain.jd_library import JobDescriptionRepository, parsed_jd
from interview.prompts.structured import StructuredOutputError
from interview.providers.base import LLMProvider

router = APIRouter(prefix="/api/jd", tags=["jd"])
log = structlog.get_logger(__name__)


class JDParseOut(JDParsed):
    jd_id: uuid.UUID
    reused: bool  # True：这份 JD 之前解析过，直接复用了结果


class SavedJDSummary(BaseModel):
    id: uuid.UUID
    title: str
    text: str
    updated_at: datetime


@router.post("/parse")
async def parse_jd_route(
    body: JDText,
    user_id: CurrentUserId,
    llm: Annotated[LLMProvider, Depends(get_jd_llm)],
    resources: Annotated[Resources, Depends(get_resources)],
) -> JDParseOut:
    try:
        saved = await parsed_jd(resources.sessionmaker, llm, user_id=user_id, text=body.text)
    except StructuredOutputError as exc:
        log.warning("jd_parse_failed", attempts=exc.attempts)
        raise AppError(
            "职位描述解析失败，请稍后重试", code="parse_failed", status_code=502
        ) from exc
    return JDParseOut(**saved.parsed.model_dump(), jd_id=saved.id, reused=saved.reused)


@router.get("/saved")
async def list_saved_jds(
    user_id: CurrentUserId,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[SavedJDSummary]:
    """解析过的 JD，最近用过的在前。"""
    query = (
        JobDescriptionRepository(session, user_id)
        .select()
        .order_by(JobDescription.updated_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(query)).scalars().all()
    return [
        SavedJDSummary(id=row.id, title=row.title or "", text=row.text, updated_at=row.updated_at)
        for row in rows
    ]
