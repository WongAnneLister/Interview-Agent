"""创建一场面试：解析 JD → 生成计划 → 在一个事务里写 interviews 和 plans。

接口（POST /api/interviews）和离线评测（evals/）共用；模型调用期间不占数据库会话。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.db.models import (
    Interview,
    InterviewStatus,
    InterviewStyle,
    InterviewType,
    Language,
    Plan,
)
from interview.domain.jd import parse_jd
from interview.domain.plan import (
    InterviewPlan,
    InterviewRepository,
    PlanRepository,
    generate_plan,
)
from interview.domain.resume import ResumeParsed
from interview.prompts.structured import StructuredResult
from interview.providers.base import LLMProvider


@dataclass(frozen=True)
class InterviewSettings:
    resume_id: uuid.UUID
    jd_text: str
    type: InterviewType
    style: InterviewStyle
    language: Language
    duration_min: int


async def build_plan(
    *,
    jd_llm: LLMProvider,
    plan_llm: LLMProvider,
    resume: ResumeParsed,
    settings: InterviewSettings,
    interview_id: uuid.UUID,
) -> StructuredResult[InterviewPlan]:
    """解析 JD 并生成计划；结构化输出失败时抛 StructuredOutputError，由调用方处理。"""
    jd = (await parse_jd(jd_llm, settings.jd_text, interview_id=interview_id)).value
    return await generate_plan(
        plan_llm,
        jd=jd,
        resume=resume,
        interview_type=settings.type,
        style=settings.style,
        language=settings.language,
        duration_min=settings.duration_min,
        interview_id=interview_id,
    )


async def persist_interview(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID,
    interview_id: uuid.UUID,
    settings: InterviewSettings,
    plan: InterviewPlan,
) -> tuple[Interview, Plan]:
    """在一个事务里写入面试和计划，返回刷新后的两行。"""
    interview = Interview(
        id=interview_id,
        user_id=user_id,
        resume_id=settings.resume_id,
        jd_text=settings.jd_text,
        type=settings.type,
        style=settings.style,
        language=settings.language,
        duration_min=settings.duration_min,
        status=InterviewStatus.PLANNED,
    )
    row = Plan(
        interview_id=interview_id,
        user_id=user_id,
        competencies_json=[item.model_dump(mode="json") for item in plan.competencies],
        questions_json=[item.model_dump(mode="json") for item in plan.questions],
        tech_terms_json=list(plan.tech_terms),
        stages_json=[item.model_dump(mode="json") for item in plan.stages],
        prompt_version=plan.prompt_version,
    )
    async with sessionmaker() as session:
        InterviewRepository(session, user_id).add(interview)
        PlanRepository(session, user_id).add(row)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        await session.refresh(interview)
        await session.refresh(row)
        return interview, row
