"""面试计划：阶段时间分配、能力项覆盖、题目生成与校验。

- 阶段时长由代码计算，不交给模型：以 20 分钟技术面为基准等比缩放，用最大余数法
  取整到秒，保证各阶段之和正好等于总时长。
- 题目由提示词 plan_questions v1 生成。每个有能力项的阶段容量为 seconds // 180
  （每题含追问按 3 分钟估算）。
- 模型输出用 PlanDraft 的 model_validator(mode="after") 校验，PlanContext 通过
  Pydantic 的 validation context 传入：不合格时抛 ValueError，由 generate_structured
  把错误反馈给模型重试；题量超出容量时先裁剪再检查覆盖，裁剪不触发重试。
- 用户 PATCH 时复用 check_questions()，只校验不裁剪。
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self, cast

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from interview.db.models import (
    Interview,
    InterviewStyle,
    InterviewType,
    Language,
    Plan,
    UsageRole,
)
from interview.db.repository import UserScopedRepository
from interview.domain.jd import Competency, JDParsed
from interview.domain.resume import ResumeParsed
from interview.observability.tracing import span
from interview.prompts.loader import get_prompt_library
from interview.prompts.structured import StructuredResult, generate_structured
from interview.providers.base import LLMProvider

BASELINE_MINUTES = 20
SECONDS_PER_MINUTE = 60
SECONDS_PER_QUESTION = 180
MAX_QUESTIONS_PER_COMPETENCY = 2
MAX_DEEP_DIVE_PROJECTS = 3
PLAN_PROMPT_ID = "plan_questions"
ALLOWED_DURATIONS = (15, 20, 30)

log = structlog.get_logger(__name__)


class Stage(StrEnum):
    """与 PR-16 状态机的阶段名一致。"""

    OPENING = "opening"
    SELF_INTRO = "self_intro"
    RESUME_DEEP_DIVE = "resume_deep_dive"
    TECHNICAL = "technical"
    BEHAVIORAL = "behavioral"
    CANDIDATE_QUESTIONS = "candidate_questions"
    CLOSING = "closing"


# 20 分钟技术面的基准分钟数；行为面把 TECHNICAL 换成 BEHAVIORAL，时长相同。
_BASELINE_MINUTES: Mapping[Stage, int] = {
    Stage.OPENING: 1,
    Stage.SELF_INTRO: 2,
    Stage.RESUME_DEEP_DIVE: 6,
    Stage.TECHNICAL: 8,
    Stage.BEHAVIORAL: 8,
    Stage.CANDIDATE_QUESTIONS: 2,
    Stage.CLOSING: 1,
}
_QUESTION_STAGES = frozenset({Stage.RESUME_DEEP_DIVE, Stage.TECHNICAL, Stage.BEHAVIORAL})


class StageSlot(BaseModel):
    """一个阶段的时间片。capacity 是无题阶段为 0，有题阶段为 seconds // 180。"""

    model_config = ConfigDict(extra="ignore")

    stage: Stage
    seconds: int = Field(ge=0)
    capacity: int = Field(ge=0)


class PlanCompetency(Competency):
    """本场计划里的能力项。covered=False 表示容量不够，本场不覆盖。"""

    covered: bool


@dataclass(frozen=True)
class PlanContext:
    """校验题目所需的计划上下文；由 build_context() / context_from_plan() 构造。"""

    main_stage: Stage
    covered: tuple[str, ...]
    deep_dive_capacity: int
    main_capacity: int
    project_names: tuple[str, ...] = ()
    allow_empty_project: bool = False
    verify_projects: bool = True


class PlanValidationError(ValueError):
    """题目不满足计划约束：生成时触发重试，PATCH 时返回 422。"""


class QuestionBody(BaseModel):
    """题目中与模型输出、用户修改共用的字段。"""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    stage: Stage
    competency: str = Field(min_length=1)
    project: str = ""
    text: str = Field(min_length=1)
    key_points: list[str] = Field(min_length=1)
    follow_ups: list[str] = Field(min_length=2, max_length=3)

    @field_validator("key_points", "follow_ups")
    @classmethod
    def non_blank(cls, values: list[str]) -> list[str]:
        if any(not value for value in values):
            raise ValueError("要点和追问不能为空白")
        return values


class DraftQuestion(QuestionBody):
    """模型输出里的题目：没有 id，id 由代码生成。"""


class Question(QuestionBody):
    """计划里保存的题目。"""

    id: str = Field(pattern=r"^q[1-9]\d*$")


class QuestionEdit(BaseModel):
    """PATCH 时可修改的字段；stage、competency、project 即使传了也忽略，从已存题目取。"""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    key_points: list[str] = Field(min_length=1)
    follow_ups: list[str] = Field(min_length=2, max_length=3)

    @field_validator("key_points", "follow_ups")
    @classmethod
    def non_blank(cls, values: list[str]) -> list[str]:
        if any(not value for value in values):
            raise ValueError("要点和追问不能为空白")
        return values


class PlanUpdate(BaseModel):
    """整份替换 questions 的请求体。"""

    questions: list[QuestionEdit]


class PlanDraft(BaseModel):
    """模型输出结构：questions 每项不含 id，其余字段同 Question。"""

    model_config = ConfigDict(extra="ignore")

    questions: list[DraftQuestion]

    @model_validator(mode="after")
    def validate_with_context(self, info: ValidationInfo) -> Self:
        context = cast(Mapping[str, Any] | None, info.context)
        ctx = context.get("plan") if context else None
        if not isinstance(ctx, PlanContext):
            raise ValueError("缺少面试计划的校验上下文")
        questions = validate_and_trim(self.questions, ctx)
        return self.model_copy(update={"questions": questions})


class InterviewPlan(BaseModel):
    """保存到 plans 表、也直接作为 API 返回的完整计划。"""

    model_config = ConfigDict(extra="ignore")

    stages: list[StageSlot]
    competencies: list[PlanCompetency]
    questions: list[Question]
    tech_terms: list[str]
    prompt_version: str


class InterviewRepository(UserScopedRepository[Interview]):
    model = Interview


class PlanRepository(UserScopedRepository[Plan]):
    model = Plan


def main_stage_for(interview_type: InterviewType) -> Stage:
    return Stage.TECHNICAL if interview_type == InterviewType.TECHNICAL else Stage.BEHAVIORAL


def allocate_stages(duration_min: int, type: InterviewType) -> list[StageSlot]:
    """按 20 分钟技术面等比缩放到指定时长，最大余数法取整，各阶段之和等于总时长。"""
    if duration_min not in ALLOWED_DURATIONS:
        raise ValueError("面试时长只能是 15、20 或 30 分钟")
    main_stage = main_stage_for(type)
    order = [
        Stage.OPENING,
        Stage.SELF_INTRO,
        Stage.RESUME_DEEP_DIVE,
        main_stage,
        Stage.CANDIDATE_QUESTIONS,
        Stage.CLOSING,
    ]
    total_seconds = duration_min * SECONDS_PER_MINUTE
    exact = [_BASELINE_MINUTES[stage] * total_seconds / BASELINE_MINUTES for stage in order]
    seconds = [int(value) for value in exact]
    remaining = total_seconds - sum(seconds)
    # 余数大的先补一秒；余数相同时保持阶段顺序，保证结果确定。
    ranked = sorted(
        range(len(exact)),
        key=lambda index: (-(exact[index] - seconds[index]), index),
    )
    for index in ranked[:remaining]:
        seconds[index] += 1
    return [
        StageSlot(
            stage=stage,
            seconds=value,
            capacity=value // SECONDS_PER_QUESTION if stage in _QUESTION_STAGES else 0,
        )
        for stage, value in zip(order, seconds, strict=True)
    ]


def select_covered(competencies: Sequence[Competency], capacity: int) -> list[PlanCompetency]:
    """按权重从高到低取前 capacity 个（并列保持 JD 原顺序），其余 covered=False。"""
    ranked = sorted(
        range(len(competencies)),
        key=lambda index: (-competencies[index].weight, index),
    )
    keep = set(ranked[: max(capacity, 0)])
    return [
        PlanCompetency(
            name=item.name,
            description=item.description,
            weight=item.weight,
            probes=list(item.probes),
            covered=index in keep,
        )
        for index, item in enumerate(competencies)
    ]


def _priority_order(competencies: Sequence[PlanCompetency]) -> tuple[str, ...]:
    """covered 能力项按权重从高到低；并列保持原顺序。"""
    return tuple(
        item.name
        for item in sorted(
            (item for item in competencies if item.covered),
            key=lambda item: -item.weight,
        )
    )


def deep_dive_quota(stages: Sequence[StageSlot], resume: ResumeParsed) -> int:
    """深挖题上限：每个项目或公司最多一道，最多 3 个，至少 1 道（简历为空时 project 留空）。"""
    capacity = next((s.capacity for s in stages if s.stage is Stage.RESUME_DEEP_DIVE), 0)
    sources = len(resume.projects) + len(resume.work)
    return max(1, min(capacity, MAX_DEEP_DIVE_PROJECTS, sources))


def _make_context(
    competencies: Sequence[PlanCompetency],
    stages: Sequence[StageSlot],
    main_stage: Stage,
    *,
    deep_dive_capacity: int | None = None,
    project_names: tuple[str, ...],
    allow_empty_project: bool,
    verify_projects: bool,
) -> PlanContext:
    capacities = {slot.stage: slot.capacity for slot in stages}
    if deep_dive_capacity is None:
        deep_dive_capacity = capacities.get(Stage.RESUME_DEEP_DIVE, 0)
    return PlanContext(
        main_stage=main_stage,
        covered=_priority_order(competencies),
        deep_dive_capacity=deep_dive_capacity,
        main_capacity=capacities.get(main_stage, 0),
        project_names=project_names,
        allow_empty_project=allow_empty_project,
        verify_projects=verify_projects,
    )


def build_context(
    competencies: Sequence[PlanCompetency],
    stages: Sequence[StageSlot],
    resume: ResumeParsed,
    main_stage: Stage,
) -> PlanContext:
    """生成题目时的上下文：项目名来自解析后的简历，深挖上限用 deep_dive_quota()。"""
    project_names = tuple(
        name
        for name in [
            *(item.name for item in resume.projects),
            *(item.company for item in resume.work),
        ]
        if name
    )
    return _make_context(
        competencies,
        stages,
        main_stage,
        deep_dive_capacity=deep_dive_quota(stages, resume),
        project_names=project_names,
        allow_empty_project=not project_names,
        verify_projects=True,
    )


def context_from_plan(
    competencies: Sequence[PlanCompetency],
    stages: Sequence[StageSlot],
    main_stage: Stage,
) -> PlanContext:
    """PATCH 时的上下文：stage / competency / project 都来自已存题目，不再校验简历。"""
    return _make_context(
        competencies,
        stages,
        main_stage,
        project_names=(),
        allow_empty_project=True,
        verify_projects=False,
    )


def _normalize(value: str) -> str:
    """项目名比较：去空白、忽略大小写。"""
    return "".join(value.split()).casefold()


def _reject_invalid(questions: Sequence[QuestionBody], ctx: PlanContext) -> None:
    allowed_stages = {Stage.RESUME_DEEP_DIVE, ctx.main_stage}
    covered = set(ctx.covered)
    project_names = {_normalize(name) for name in ctx.project_names}
    seen_projects: set[str] = set()
    for question in questions:
        if question.competency not in covered:
            raise PlanValidationError(f"题目引用了未覆盖的能力项：{question.competency}")
        if question.stage not in allowed_stages:
            raise PlanValidationError(f"题目阶段不合法：{question.stage}")
        if question.stage is not Stage.RESUME_DEEP_DIVE or not ctx.verify_projects:
            continue
        key = _normalize(question.project)
        if not key:
            if not ctx.allow_empty_project:
                raise PlanValidationError("深挖题必须对应简历里的项目或公司")
            continue
        if key not in project_names:
            raise PlanValidationError(f"深挖题的项目不在简历里：{question.project}")
        if key in seen_projects:
            raise PlanValidationError(f"同一个项目不能出现两道深挖题：{question.project}")
        seen_projects.add(key)


def _ensure_coverage(questions: Sequence[QuestionBody], ctx: PlanContext) -> None:
    present = {question.competency for question in questions}
    missing = [name for name in ctx.covered if name not in present]
    if missing:
        raise PlanValidationError("以下能力项没有题目：" + "、".join(missing))


def _ensure_limits(questions: Sequence[QuestionBody], ctx: PlanContext) -> None:
    per_competency = Counter(question.competency for question in questions)
    for name, count in per_competency.items():
        if count > MAX_QUESTIONS_PER_COMPETENCY:
            raise PlanValidationError(
                f"能力项「{name}」的题目超过 {MAX_QUESTIONS_PER_COMPETENCY} 道"
            )
    per_stage = Counter(question.stage for question in questions)
    capacities = {
        Stage.RESUME_DEEP_DIVE: ctx.deep_dive_capacity,
        ctx.main_stage: ctx.main_capacity,
    }
    for stage, capacity in capacities.items():
        if per_stage[stage] > capacity:
            raise PlanValidationError(f"{stage.value} 阶段的题目超过容量 {capacity}")


def _trim[Q: QuestionBody](questions: Sequence[Q], ctx: PlanContext) -> list[Q]:
    """每个能力项最多 2 题；阶段超容量时先保留每个能力项的第一题（按权重），再按原顺序补满。"""
    counted: Counter[str] = Counter()
    capped: list[Q] = []
    for question in questions:
        if counted[question.competency] >= MAX_QUESTIONS_PER_COMPETENCY:
            continue
        counted[question.competency] += 1
        capped.append(question)

    first: dict[str, Q] = {}
    for question in capped:
        first.setdefault(question.competency, question)

    trimmed: list[Q] = []
    for stage, capacity in (
        (Stage.RESUME_DEEP_DIVE, ctx.deep_dive_capacity),
        (ctx.main_stage, ctx.main_capacity),
    ):
        items = [question for question in capped if question.stage is stage]
        if len(items) > capacity:
            kept: set[int] = set()
            for name in ctx.covered:
                if len(kept) >= capacity:
                    break
                candidate = first.get(name)
                if candidate is not None and candidate.stage is stage:
                    kept.add(id(candidate))
            for question in items:
                if len(kept) >= capacity:
                    break
                kept.add(id(question))
            items = [question for question in items if id(question) in kept]
        trimmed.extend(items)
    return trimmed


def validate_and_trim[Q: QuestionBody](questions: Sequence[Q], ctx: PlanContext) -> list[Q]:
    """生成时的校验：先拒绝明确错误，再裁剪，最后检查覆盖。"""
    _reject_invalid(questions, ctx)
    trimmed = _trim(questions, ctx)
    _ensure_coverage(trimmed, ctx)
    return trimmed


def check_questions(questions: Sequence[Question], ctx: PlanContext) -> None:
    """PATCH 时的重新校验：不裁剪，超容量、题目过多或覆盖缺失都抛 PlanValidationError。"""
    _reject_invalid(questions, ctx)
    _ensure_limits(questions, ctx)
    _ensure_coverage(questions, ctx)


def apply_question_edits(
    existing: Sequence[Question], edits: Sequence[QuestionEdit]
) -> list[Question]:
    """整份替换题目：id 必须对应已存题目且不重复；stage / competency / project 保持原值。"""
    by_id = {question.id: question for question in existing}
    seen: set[str] = set()
    updated: list[Question] = []
    for edit in edits:
        if edit.id in seen:
            raise PlanValidationError(f"题目 id 重复：{edit.id}")
        base = by_id.get(edit.id)
        if base is None:
            raise PlanValidationError(f"未知的题目 id：{edit.id}")
        seen.add(edit.id)
        updated.append(
            Question(
                id=base.id,
                stage=base.stage,
                competency=base.competency,
                project=base.project,
                text=edit.text,
                key_points=list(edit.key_points),
                follow_ups=list(edit.follow_ups),
            )
        )
    return updated


def _with_ids(questions: Sequence[DraftQuestion]) -> list[Question]:
    return [
        Question(
            id=f"q{index}",
            stage=item.stage,
            competency=item.competency,
            project=item.project,
            text=item.text,
            key_points=list(item.key_points),
            follow_ups=list(item.follow_ups),
        )
        for index, item in enumerate(questions, start=1)
    ]


async def generate_plan(
    llm: LLMProvider,
    *,
    jd: JDParsed,
    resume: ResumeParsed,
    interview_type: InterviewType,
    style: InterviewStyle,
    language: Language,
    duration_min: int,
    interview_id: uuid.UUID,
) -> StructuredResult[InterviewPlan]:
    """生成一份面试计划；span 名为 plan.generate，用量由调用方的 CallContext 记为 plan。"""
    stages = allocate_stages(duration_min, interview_type)
    main_stage = main_stage_for(interview_type)
    main_capacity = next(s.capacity for s in stages if s.stage is main_stage)
    # 覆盖容量按实际能出的深挖题算，避免项目太少时 covered 能力项永远凑不齐。
    competencies = select_covered(jd.competencies, deep_dive_quota(stages, resume) + main_capacity)
    ctx = build_context(competencies, stages, resume, main_stage)
    by_name = {item.name: item for item in competencies if item.covered}
    covered = [by_name[name] for name in ctx.covered]
    prompt = get_prompt_library().render(
        PLAN_PROMPT_ID,
        job_title=jd.title,
        job_level=jd.level,
        main_stage=main_stage.value,
        language=language.value,
        style=style.value,
        deep_dive_capacity=ctx.deep_dive_capacity,
        main_capacity=ctx.main_capacity,
        has_projects=bool(ctx.project_names),
        competencies=[
            {"name": item.name, "description": item.description, "probes": item.probes}
            for item in covered
        ],
        resume=resume.model_dump_json(),
    )
    with span("plan.generate", interview_id=str(interview_id), role=UsageRole.PLAN.value):
        result = await generate_structured(
            llm,
            prompt,
            PlanDraft,
            max_tokens=4096,
            context={"plan": ctx},
        )
    plan = InterviewPlan(
        stages=stages,
        competencies=competencies,
        questions=_with_ids(result.value.questions),
        tech_terms=list(jd.tech_terms),
        prompt_version=f"{result.prompt_id}@{result.prompt_version}",
    )
    log.info(
        "plan_generated",
        interview_id=str(interview_id),
        questions=len(plan.questions),
        covered=sum(1 for item in competencies if item.covered),
    )
    return StructuredResult(
        plan, result.prompt_id, result.prompt_version, result.retries, result.provider, result.model
    )
