"""面试创建与计划查看、修改。

- POST /api/interviews：读取简历 → 解析 JD → 生成计划 → 在一个事务里写 interviews
  和 plans。模型调用期间不占数据库会话。
- GET /api/interviews/{id}/plan：返回完整计划。
- PATCH /api/interviews/{id}/plan：整份替换 questions，只能改文字、要点和追问；
  不能新增题目，面试不是 planned 状态时返回 409。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator

from interview.api.deps import (
    CurrentUserId,
    DbSession,
    Providers,
    eval_llm,
    get_resources,
)
from interview.api.errors import AppError, ConflictError, NotFoundError
from interview.api.resources import Resources
from interview.db.models import (
    Interview,
    InterviewStatus,
    InterviewStyle,
    InterviewType,
    Language,
    Plan,
    UsageRole,
)
from interview.domain.jd import JDText, parse_jd
from interview.domain.plan import (
    InterviewPlan,
    InterviewRepository,
    PlanCompetency,
    PlanRepository,
    PlanUpdate,
    PlanValidationError,
    Question,
    StageSlot,
    apply_question_edits,
    check_questions,
    context_from_plan,
    generate_plan,
    main_stage_for,
)
from interview.domain.resume import ResumeParsed, ResumeRepository
from interview.prompts.structured import StructuredOutputError
from interview.providers.observed import CallContext

router = APIRouter(prefix="/api/interviews", tags=["interviews"])
log = structlog.get_logger(__name__)


class InterviewCreate(BaseModel):
    resume_id: uuid.UUID
    jd_text: str
    type: InterviewType
    style: InterviewStyle
    language: Language
    duration_min: Literal[15, 20, 30]

    @field_validator("jd_text")
    @classmethod
    def valid_jd_text(cls, value: str) -> str:
        return JDText(text=value).text


class PlanOut(BaseModel):
    stages: list[StageSlot]
    competencies: list[PlanCompetency]
    questions: list[Question]
    tech_terms: list[str]
    prompt_version: str
    updated_at: datetime


class InterviewOut(BaseModel):
    id: uuid.UUID
    resume_id: uuid.UUID
    status: InterviewStatus
    type: InterviewType
    style: InterviewStyle
    language: Language
    duration_min: int
    created_at: datetime
    updated_at: datetime
    plan: PlanOut


def _plan_view(plan: Plan) -> PlanOut:
    return PlanOut(
        stages=[StageSlot.model_validate(item) for item in plan.stages_json],
        competencies=[PlanCompetency.model_validate(item) for item in plan.competencies_json],
        questions=[Question.model_validate(item) for item in plan.questions_json],
        tech_terms=list(plan.tech_terms_json),
        prompt_version=plan.prompt_version,
        updated_at=plan.updated_at,
    )


def _interview_view(interview: Interview, plan: Plan) -> InterviewOut:
    return InterviewOut(
        id=interview.id,
        resume_id=interview.resume_id,
        status=interview.status,
        type=interview.type,
        style=interview.style,
        language=interview.language,
        duration_min=interview.duration_min,
        created_at=interview.created_at,
        updated_at=interview.updated_at,
        plan=_plan_view(plan),
    )


async def _load_resume(
    resources: Resources, user_id: uuid.UUID, resume_id: uuid.UUID
) -> ResumeParsed:
    """短会话读取简历，读完立即关闭；模型调用期间不占数据库会话。"""
    async with resources.sessionmaker() as session:
        resume = await ResumeRepository(session, user_id).get(resume_id)
        if resume is None or resume.user_id != user_id:
            raise NotFoundError("简历不存在")
        parsed = resume.parsed_json
    if not isinstance(parsed, dict):
        raise NotFoundError("简历不存在")
    return ResumeParsed.model_validate(parsed)


async def _owned_interview(
    session: DbSession, user_id: uuid.UUID, interview_id: uuid.UUID
) -> Interview:
    interview = await InterviewRepository(session, user_id).get(interview_id)
    if interview is None or interview.user_id != user_id:
        raise NotFoundError("面试不存在")
    return interview


async def _owned_plan(session: DbSession, user_id: uuid.UUID, interview_id: uuid.UUID) -> Plan:
    plan = await PlanRepository(session, user_id).get(interview_id)
    if plan is None or plan.user_id != user_id:
        raise NotFoundError("面试计划不存在")
    return plan


async def _persist(
    resources: Resources,
    user_id: uuid.UUID,
    interview_id: uuid.UUID,
    body: InterviewCreate,
    plan: InterviewPlan,
) -> InterviewOut:
    interview = Interview(
        id=interview_id,
        user_id=user_id,
        resume_id=body.resume_id,
        jd_text=body.jd_text,
        type=body.type,
        style=body.style,
        language=body.language,
        duration_min=body.duration_min,
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
    async with resources.sessionmaker() as session:
        InterviewRepository(session, user_id).add(interview)
        PlanRepository(session, user_id).add(row)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        await session.refresh(interview)
        await session.refresh(row)
        return _interview_view(interview, row)


@router.post("", status_code=201)
async def create_interview(
    body: InterviewCreate,
    user_id: CurrentUserId,
    providers: Providers,
    resources: Annotated[Resources, Depends(get_resources)],
) -> InterviewOut:
    resume = await _load_resume(resources, user_id, body.resume_id)
    interview_id = uuid.uuid4()
    jd_llm = eval_llm(
        providers,
        CallContext(
            user_id=user_id,
            interview_id=interview_id,
            usage_role=UsageRole.JD_PARSE,
        ),
        message="评估模型未配置，无法解析职位描述",
    )
    plan_llm = eval_llm(
        providers,
        CallContext(
            user_id=user_id,
            interview_id=interview_id,
            usage_role=UsageRole.PLAN,
        ),
        message="评估模型未配置，无法生成面试计划",
    )
    try:
        jd = (await parse_jd(jd_llm, body.jd_text, interview_id=interview_id)).value
        result = await generate_plan(
            plan_llm,
            jd=jd,
            resume=resume,
            interview_type=body.type,
            style=body.style,
            language=body.language,
            duration_min=body.duration_min,
            interview_id=interview_id,
        )
    except StructuredOutputError as exc:
        log.warning(
            "interview_plan_failed",
            interview_id=str(interview_id),
            attempts=exc.attempts,
        )
        raise AppError("面试计划生成失败，请稍后重试", code="plan_failed", status_code=502) from exc
    view = await _persist(resources, user_id, interview_id, body, result.value)
    log.info(
        "interview_created",
        interview_id=str(interview_id),
        duration_min=body.duration_min,
        questions=len(result.value.questions),
        retries=result.retries,
    )
    return view


@router.get("/{interview_id}/plan")
async def get_plan(
    interview_id: uuid.UUID,
    user_id: CurrentUserId,
    session: DbSession,
) -> PlanOut:
    await _owned_interview(session, user_id, interview_id)
    return _plan_view(await _owned_plan(session, user_id, interview_id))


@router.patch("/{interview_id}/plan")
async def update_plan(
    interview_id: uuid.UUID,
    body: PlanUpdate,
    user_id: CurrentUserId,
    session: DbSession,
) -> PlanOut:
    interview = await _owned_interview(session, user_id, interview_id)
    plan = await _owned_plan(session, user_id, interview_id)
    if interview.status != InterviewStatus.PLANNED:
        raise ConflictError("面试已开始或已结束，计划不能修改")
    stored = [Question.model_validate(item) for item in plan.questions_json]
    try:
        questions = apply_question_edits(stored, body.questions)
        check_questions(
            questions,
            context_from_plan(
                [PlanCompetency.model_validate(item) for item in plan.competencies_json],
                [StageSlot.model_validate(item) for item in plan.stages_json],
                main_stage_for(interview.type),
            ),
        )
    except PlanValidationError as exc:
        raise AppError(str(exc), code="plan_invalid", status_code=422) from exc
    plan.questions_json = [item.model_dump(mode="json") for item in questions]
    plan.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(plan)
    return _plan_view(plan)
