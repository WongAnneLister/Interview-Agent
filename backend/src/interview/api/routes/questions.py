"""历史面试题：把每次按 JD 生成的计划里的题目汇总起来，供前端按岗位、题型、能力分组。

只读，按 user_id 限定。计划被修改过时返回修改后的题目。题目结构不合法的（旧版本数据）跳过。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel, ValidationError

from interview.api.deps import CurrentUserId, DbSession
from interview.db.models import Interview, InterviewStatus, InterviewType, Language, Plan
from interview.domain.plan import InterviewRepository, Question

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/questions", tags=["questions"])


class HistoryQuestion(BaseModel):
    interview_id: uuid.UUID
    # 岗位名称；JD 没注明或旧面试没存时为空（旧面试可用 make backfill-job-titles 补）
    job_title: str
    interview_type: InterviewType
    language: Language
    status: InterviewStatus
    created_at: datetime  # 这场面试（也就是这批题目）生成的时间
    question: Question


@router.get("")
async def list_questions(
    user_id: CurrentUserId,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200, description="最多取最近多少场面试的题目")] = 50,
) -> list[HistoryQuestion]:
    query = (
        InterviewRepository(session, user_id)
        .select()
        .add_columns(Plan)
        .join(Plan, (Plan.interview_id == Interview.id) & (Plan.user_id == user_id))
        .order_by(Interview.created_at.desc())
        .limit(limit)
    )
    result: list[HistoryQuestion] = []
    skipped = 0
    for interview, plan in (await session.execute(query)).tuples():
        title = interview.jd_title or ""
        for raw in plan.questions_json:
            try:
                question = Question.model_validate(raw)
            except ValidationError:
                skipped += 1
                continue
            result.append(
                HistoryQuestion(
                    interview_id=interview.id,
                    job_title=title,
                    interview_type=interview.type,
                    language=interview.language,
                    status=interview.status,
                    created_at=interview.created_at,
                    question=question,
                )
            )
    if skipped:
        log.warning("history_questions_skipped", skipped=skipped)
    return result
