"""面试计划：时间分配、能力项覆盖、题目校验 / 裁剪与 PATCH 重新校验。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from interview.db.models import InterviewStyle, InterviewType, Language
from interview.domain.jd import JDParsed
from interview.domain.plan import (
    MAX_QUESTIONS_PER_COMPETENCY,
    InterviewPlan,
    PlanCompetency,
    PlanContext,
    PlanDraft,
    PlanValidationError,
    Question,
    QuestionEdit,
    Stage,
    StageSlot,
    allocate_stages,
    apply_question_edits,
    build_context,
    check_questions,
    deep_dive_quota,
    generate_plan,
    main_stage_for,
    select_covered,
)
from interview.domain.resume import ResumeParsed
from interview.prompts.structured import StructuredOutputError
from interview.providers.fake import FakeLLM

INTERVIEW_ID = uuid.UUID("00000000-0000-4000-8000-000000000015")

JD_DATA: dict[str, Any] = {
    "title": "后端工程师",
    "level": "高级",
    "competencies": [
        {"name": "后端开发", "description": "服务开发", "weight": 4, "probes": ["性能", "稳定性"]},
        {"name": "系统设计", "description": "架构设计", "weight": 3, "probes": ["拆分", "扩展"]},
        {"name": "数据库", "description": "数据建模", "weight": 2, "probes": ["索引", "事务"]},
        {"name": "协作", "description": "团队合作", "weight": 1, "probes": ["沟通", "推动"]},
    ],
    "tech_terms": ["Go", "PostgreSQL"],
}

RESUME_DATA: dict[str, Any] = {
    "name": "Alex Chen",
    "work": [
        {
            "company": "Northwind Labs",
            "title": "Backend Engineer",
            "highlights": ["Built a payment service"],
        }
    ],
    "projects": [
        {
            "name": "Ledger",
            "role": "owner",
            "tech_stack": ["Go", "PostgreSQL"],
            "highlights": ["Cut checkout latency"],
        }
    ],
    "skills": ["Go", "PostgreSQL"],
    "education": [],
}

JD = JDParsed.model_validate(JD_DATA)
RESUME = ResumeParsed.model_validate(RESUME_DATA)


def draft_question(
    stage: Stage | str,
    competency: str,
    *,
    project: str = "",
    text: str | None = None,
    key_points: Sequence[str] = ("要点",),
    follow_ups: Sequence[str] = ("追问一", "追问二"),
) -> dict[str, Any]:
    stage_value = stage.value if isinstance(stage, Stage) else stage
    return {
        "stage": stage_value,
        "competency": competency,
        "project": project,
        "text": text or f"{competency}-{stage_value}",
        "key_points": list(key_points),
        "follow_ups": list(follow_ups),
    }


def good_questions() -> list[dict[str, Any]]:
    """20 分钟技术面的合法草稿：2 道深挖 + 2 道主阶段，覆盖 4 个能力项。"""
    return [
        draft_question(Stage.RESUME_DEEP_DIVE, "后端开发", project="Ledger", text="深挖 Ledger"),
        draft_question(
            Stage.RESUME_DEEP_DIVE, "系统设计", project="Northwind Labs", text="深挖 Northwind"
        ),
        draft_question(Stage.TECHNICAL, "数据库", text="数据库题"),
        draft_question(Stage.TECHNICAL, "协作", text="协作题"),
    ]


def context_for(
    duration_min: int,
    interview_type: InterviewType = InterviewType.TECHNICAL,
    *,
    jd: JDParsed = JD,
    resume: ResumeParsed = RESUME,
) -> tuple[list[PlanCompetency], list[StageSlot], PlanContext]:
    stages = allocate_stages(duration_min, interview_type)
    main_capacity = next(s.capacity for s in stages if s.stage is main_stage_for(interview_type))
    covered = select_covered(jd.competencies, deep_dive_quota(stages, resume) + main_capacity)
    return covered, stages, build_context(covered, stages, resume, main_stage_for(interview_type))


def draft_with(questions: list[dict[str, Any]], ctx: PlanContext) -> PlanDraft:
    return PlanDraft.model_validate_json(
        json.dumps({"questions": questions}, ensure_ascii=False),
        context={"plan": ctx},
    )


def stored_questions() -> list[Question]:
    return [
        Question(id=f"q{index}", **item) for index, item in enumerate(good_questions(), start=1)
    ]


# ---- 时间分配 ----


@pytest.mark.parametrize(
    ("duration", "interview_type", "seconds", "deep", "main", "deep_capacity", "main_capacity"),
    [
        (
            15,
            InterviewType.TECHNICAL,
            [45, 90, 270, 360, 90, 45],
            270,
            360,
            1,
            2,
        ),
        (
            20,
            InterviewType.TECHNICAL,
            [60, 120, 360, 480, 120, 60],
            360,
            480,
            2,
            2,
        ),
        (
            30,
            InterviewType.TECHNICAL,
            [90, 180, 540, 720, 180, 90],
            540,
            720,
            3,
            4,
        ),
        (
            15,
            InterviewType.BEHAVIORAL,
            [45, 90, 270, 360, 90, 45],
            270,
            360,
            1,
            2,
        ),
        (
            30,
            InterviewType.BEHAVIORAL,
            [90, 180, 540, 720, 180, 90],
            540,
            720,
            3,
            4,
        ),
    ],
)
def test_allocate_stages(
    duration: int,
    interview_type: InterviewType,
    seconds: list[int],
    deep: int,
    main: int,
    deep_capacity: int,
    main_capacity: int,
) -> None:
    stages = allocate_stages(duration, interview_type)
    assert [slot.seconds for slot in stages] == seconds
    assert sum(slot.seconds for slot in stages) == duration * 60
    by_stage = {slot.stage: slot for slot in stages}
    main_stage = main_stage_for(interview_type)
    assert (by_stage[Stage.RESUME_DEEP_DIVE].seconds, by_stage[main_stage].seconds) == (deep, main)
    assert (by_stage[Stage.RESUME_DEEP_DIVE].capacity, by_stage[main_stage].capacity) == (
        deep_capacity,
        main_capacity,
    )
    for stage in (Stage.OPENING, Stage.SELF_INTRO, Stage.CANDIDATE_QUESTIONS, Stage.CLOSING):
        assert by_stage[stage].capacity == 0


def test_allocate_stages_rejects_unsupported_duration() -> None:
    with pytest.raises(ValueError, match="15、20 或 30"):
        allocate_stages(10, InterviewType.TECHNICAL)


# ---- 能力项覆盖 ----


def test_select_covered_breaks_weight_ties_by_jd_order() -> None:
    competencies = select_covered(JD.competencies, 2)
    # 权重：后端开发 0.4、系统设计 0.3、数据库 0.2、协作 0.1，取前 2。
    assert [item.name for item in competencies] == ["后端开发", "系统设计", "数据库", "协作"]
    assert [item.covered for item in competencies] == [True, True, False, False]


def test_select_covered_keeps_jd_order_for_equal_weights() -> None:
    jd = JDParsed.model_validate(
        {
            "title": "后端工程师",
            "level": "",
            "competencies": [
                {"name": "A", "description": "d", "weight": 1, "probes": ["p"]},
                {"name": "B", "description": "d", "weight": 1, "probes": ["p"]},
                {"name": "C", "description": "d", "weight": 1, "probes": ["p"]},
            ],
            "tech_terms": [],
        }
    )
    covered = select_covered(jd.competencies, 2)
    assert [item.covered for item in covered] == [True, True, False]


# ---- 生成与重试 ----


def _unknown_competency(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items[0]["competency"] = "不存在的能力项"
    return items


def _wrong_stage(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items[0]["stage"] = "opening"
    return items


def _fabricated_project(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items[0]["project"] = "Fake Project"
    return items


def _duplicate_project(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items[1]["project"] = "Ledger"
    return items


def _missing_coverage(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if item["competency"] != "协作"]


BAD_DRAFTS: list[Callable[[list[dict[str, Any]]], list[dict[str, Any]]]] = [
    _unknown_competency,
    _wrong_stage,
    _fabricated_project,
    _duplicate_project,
    _missing_coverage,
]


@pytest.mark.parametrize(
    "mutate",
    BAD_DRAFTS,
    ids=[
        "unknown-competency",
        "wrong-stage",
        "fabricated-project",
        "duplicate-project",
        "missing-coverage",
    ],
)
async def test_invalid_draft_is_retried(
    mutate: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> None:
    bad = mutate(deepcopy(good_questions()))
    replies = iter(
        [
            json.dumps({"questions": bad}, ensure_ascii=False),
            json.dumps({"questions": good_questions()}, ensure_ascii=False),
        ]
    )
    llm = FakeLLM(reply=lambda _messages: next(replies), chunk_size=10000)
    result = await generate_plan(
        llm,
        jd=JD,
        resume=RESUME,
        interview_type=InterviewType.TECHNICAL,
        style=InterviewStyle.GENTLE,
        language=Language.ZH,
        duration_min=20,
        interview_id=INTERVIEW_ID,
    )
    assert result.retries == 1
    assert len(llm.calls) == 2
    assert [question.id for question in result.value.questions] == ["q1", "q2", "q3", "q4"]


async def test_retry_exhausted_raises_structured_output_error() -> None:
    llm = FakeLLM(reply="not json")
    with pytest.raises(StructuredOutputError) as caught:
        await generate_plan(
            llm,
            jd=JD,
            resume=RESUME,
            interview_type=InterviewType.TECHNICAL,
            style=InterviewStyle.GENTLE,
            language=Language.ZH,
            duration_min=20,
            interview_id=INTERVIEW_ID,
        )
    assert caught.value.attempts == len(llm.calls) == 3


async def test_generated_plan_has_prompt_version_and_tech_terms() -> None:
    llm = FakeLLM(
        reply=json.dumps({"questions": good_questions()}, ensure_ascii=False),
        chunk_size=10000,
    )
    result = await generate_plan(
        llm,
        jd=JD,
        resume=RESUME,
        interview_type=InterviewType.TECHNICAL,
        style=InterviewStyle.GENTLE,
        language=Language.ZH,
        duration_min=20,
        interview_id=INTERVIEW_ID,
    )
    plan: InterviewPlan = result.value
    assert plan.prompt_version == "plan_questions@1"
    assert plan.tech_terms == ["Go", "PostgreSQL"]
    assert [item.covered for item in plan.competencies] == [True, True, True, True]
    assert plan.questions[0].project == "Ledger"
    assert plan.questions[2].project == ""


# ---- 裁剪 ----


def test_trim_caps_two_questions_per_competency() -> None:
    _, _, ctx = context_for(30)
    questions = [
        draft_question(Stage.RESUME_DEEP_DIVE, "后端开发", project="Ledger", text="A1"),
        draft_question(Stage.RESUME_DEEP_DIVE, "系统设计", project="Northwind Labs", text="B1"),
        draft_question(Stage.TECHNICAL, "数据库", text="C1"),
        draft_question(Stage.TECHNICAL, "数据库", text="C2"),
        draft_question(Stage.TECHNICAL, "数据库", text="C3"),
        draft_question(Stage.TECHNICAL, "协作", text="D1"),
    ]
    draft = draft_with(questions, ctx)
    texts = [question.text for question in draft.questions]
    assert "C3" not in texts
    assert texts.count("C1") == texts.count("C2") == 1
    assert sum(1 for question in draft.questions if question.competency == "数据库") == (
        MAX_QUESTIONS_PER_COMPETENCY
    )
    assert set(texts) == {"A1", "B1", "C1", "C2", "D1"}


def test_trim_keeps_first_question_of_each_competency_when_stage_is_full() -> None:
    _, _, ctx = context_for(15)
    questions = [
        draft_question(Stage.TECHNICAL, "后端开发", text="A1"),
        draft_question(Stage.TECHNICAL, "系统设计", text="B1"),
        draft_question(Stage.RESUME_DEEP_DIVE, "数据库", project="Ledger", text="C1"),
        draft_question(Stage.TECHNICAL, "数据库", text="C2"),
    ]
    draft = draft_with(questions, ctx)
    texts = [question.text for question in draft.questions]
    # 主阶段容量 2：先保留各能力项的第一题（后端开发、系统设计），数据库由深挖题覆盖。
    assert texts == ["C1", "A1", "B1"]
    assert "C2" not in texts


def test_empty_resume_allows_one_deep_dive_without_project() -> None:
    covered, _, ctx = context_for(20, resume=ResumeParsed())
    assert ctx.deep_dive_capacity == 1
    assert [item.covered for item in covered] == [True, True, True, False]
    questions = [
        draft_question(Stage.RESUME_DEEP_DIVE, "后端开发", text="深挖一"),
        draft_question(Stage.RESUME_DEEP_DIVE, "后端开发", text="深挖二"),
        draft_question(Stage.TECHNICAL, "系统设计", text="系统设计题"),
        draft_question(Stage.TECHNICAL, "数据库", text="数据库题"),
    ]
    draft = draft_with(questions, ctx)
    assert [question.text for question in draft.questions] == ["深挖一", "系统设计题", "数据库题"]
    assert all(question.project == "" for question in draft.questions)


@pytest.mark.parametrize(
    ("duration", "projects", "expected"),
    [(15, 2, 1), (20, 1, 1), (20, 2, 2), (30, 2, 2), (30, 5, 3), (20, 0, 1)],
)
def test_deep_dive_quota_follows_capacity_and_resume(
    duration: int, projects: int, expected: int
) -> None:
    resume = ResumeParsed.model_validate({"projects": [{"name": f"P{i}"} for i in range(projects)]})
    assert deep_dive_quota(allocate_stages(duration, InterviewType.TECHNICAL), resume) == expected


async def test_prompt_states_the_same_deep_dive_limit_as_validation() -> None:
    resume = ResumeParsed.model_validate({"projects": [{"name": "Ledger"}]})
    llm = FakeLLM(reply="not json")
    with pytest.raises(StructuredOutputError):
        await generate_plan(
            llm,
            jd=JD,
            resume=resume,
            interview_type=InterviewType.TECHNICAL,
            style=InterviewStyle.GENTLE,
            language=Language.ZH,
            duration_min=20,
            interview_id=INTERVIEW_ID,
        )
    system, user = (message["content"] for message in llm.calls[0]["messages"][:2])
    assert "简历深挖题最多 1 道；主阶段题最多 2 道" in user
    assert "project_count" not in system + user and "main_capacity" not in system + user


# ---- PATCH 重新校验 ----


def test_check_questions_accepts_valid_plan() -> None:
    _, _, ctx = context_for(20)
    check_questions(stored_questions(), ctx)


def test_check_questions_rejects_removing_a_covered_competency() -> None:
    _, _, ctx = context_for(20)
    partial = [question for question in stored_questions() if question.competency != "协作"]
    with pytest.raises(PlanValidationError, match="协作"):
        check_questions(partial, ctx)


def test_check_questions_rejects_stage_over_capacity() -> None:
    _, _, ctx = context_for(15)
    questions = [
        Question(
            id="q1",
            stage=Stage.TECHNICAL,
            competency="后端开发",
            text="A",
            key_points=["要点"],
            follow_ups=["追问一", "追问二"],
        ),
        Question(
            id="q2",
            stage=Stage.TECHNICAL,
            competency="系统设计",
            text="B",
            key_points=["要点"],
            follow_ups=["追问一", "追问二"],
        ),
        Question(
            id="q3",
            stage=Stage.TECHNICAL,
            competency="数据库",
            text="C",
            key_points=["要点"],
            follow_ups=["追问一", "追问二"],
        ),
    ]
    with pytest.raises(PlanValidationError, match="容量"):
        check_questions(questions, ctx)


def test_question_fields_reject_bad_follow_ups_and_empty_key_points() -> None:
    with pytest.raises(ValidationError):
        QuestionEdit(id="q1", text="题目", key_points=["要点"], follow_ups=["只有一条"])
    with pytest.raises(ValidationError):
        Question(
            id="q1",
            stage=Stage.TECHNICAL,
            competency="后端开发",
            text="题目",
            key_points=[],
            follow_ups=["追问一", "追问二"],
        )
    with pytest.raises(ValidationError):
        QuestionEdit(id="q1", text="题目", key_points=[""], follow_ups=["追问一", "追问二"])


def test_apply_question_edits_rejects_unknown_and_duplicate_ids() -> None:
    stored = stored_questions()
    edit = QuestionEdit(
        id="q1", text="改后的题", key_points=["要点"], follow_ups=["追问一", "追问二"]
    )
    with pytest.raises(PlanValidationError, match="重复"):
        apply_question_edits(stored, [edit, edit])
    with pytest.raises(PlanValidationError, match="未知"):
        apply_question_edits(
            stored,
            [
                QuestionEdit(
                    id="q99",
                    text="新题",
                    key_points=["要点"],
                    follow_ups=["追问一", "追问二"],
                )
            ],
        )


def test_apply_question_edits_keeps_stage_competency_and_project() -> None:
    updated = apply_question_edits(
        stored_questions(),
        [
            QuestionEdit(
                id="q1",
                text="改后的题",
                key_points=["新要点"],
                follow_ups=["新追问一", "新追问二"],
            )
        ],
    )
    assert len(updated) == 1
    assert (updated[0].stage, updated[0].competency, updated[0].project) == (
        Stage.RESUME_DEEP_DIVE,
        "后端开发",
        "Ledger",
    )
    assert updated[0].text == "改后的题"
