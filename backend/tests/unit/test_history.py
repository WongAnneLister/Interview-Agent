"""历史压缩：当前阶段保留、早期阶段摘要、截断、条数上限和 phase 推断。"""

from __future__ import annotations

from interview.db.models import InterviewType, Language, TurnRole
from interview.domain.history import (
    MAX_TURN_CHARS,
    TurnRecord,
    build_history,
    infer_phases,
)
from interview.domain.interviewer import MAX_HISTORY_TURNS, HistoryTurn
from interview.domain.plan import (
    InterviewPlan,
    PlanCompetency,
    Question,
    Stage,
    allocate_stages,
)
from interview.domain.state_machine import Phase


def make_plan() -> InterviewPlan:
    stages = allocate_stages(20, InterviewType.TECHNICAL)
    questions = [
        Question(
            id="q1",
            stage=Stage.RESUME_DEEP_DIVE,
            competency="后端开发",
            project="Ledger",
            text="深挖 Ledger 项目",
            key_points=["可靠投递"],
            follow_ups=["追问一", "追问二"],
        ),
        Question(
            id="q2",
            stage=Stage.RESUME_DEEP_DIVE,
            competency="系统设计",
            project="Northwind Labs",
            text="深挖 Northwind Labs 项目",
            key_points=["幂等消费"],
            follow_ups=["追问一", "追问二"],
        ),
        Question(
            id="q3",
            stage=Stage.TECHNICAL,
            competency="数据库",
            project="",
            text="数据库题",
            key_points=["索引"],
            follow_ups=["追问一", "追问二"],
        ),
        Question(
            id="q4",
            stage=Stage.TECHNICAL,
            competency="协作",
            project="",
            text="协作题",
            key_points=["沟通"],
            follow_ups=["追问一", "追问二"],
        ),
    ]
    competencies = [
        PlanCompetency(
            name=name,
            description=name,
            weight=1.0,
            probes=["要点"],
            covered=True,
        )
        for name in ("后端开发", "系统设计", "数据库", "协作")
    ]
    return InterviewPlan(
        stages=stages,
        competencies=competencies,
        questions=questions,
        tech_terms=[],
        prompt_version="plan_questions@1",
    )


def record(
    role: TurnRole, text: str, question_id: str | None = None, phase: Phase | None = None
) -> TurnRecord:
    return TurnRecord(role=role, text=text, question_id=question_id, phase=phase)


def test_current_phase_kept_and_earlier_phases_summarized() -> None:
    plan = make_plan()
    turns = [
        record(TurnRole.INTERVIEWER, "请做自我介绍", phase=Phase.SELF_INTRO),
        record(TurnRole.CANDIDATE, "我是 Alex", phase=Phase.SELF_INTRO),
        record(TurnRole.INTERVIEWER, "深挖 Ledger 项目", "q1", Phase.RESUME_DEEP_DIVE),
        record(TurnRole.CANDIDATE, "我做了 Ledger", "q1", Phase.RESUME_DEEP_DIVE),
        record(TurnRole.INTERVIEWER, "深挖 Northwind Labs 项目", "q2", Phase.RESUME_DEEP_DIVE),
        record(TurnRole.CANDIDATE, "我做了 Northwind", "q2", Phase.RESUME_DEEP_DIVE),
        record(TurnRole.INTERVIEWER, "数据库题", "q3", Phase.TECHNICAL),
        record(TurnRole.CANDIDATE, "用了索引", "q3", Phase.TECHNICAL),
    ]

    history = build_history(turns, plan, Phase.TECHNICAL, language=Language.ZH)

    assert [item.text for item in history] == [
        "（已完成：自我介绍阶段）",
        "（已完成：简历深挖阶段，问了 2 道题：深挖 Ledger 项目、深挖 Northwind Labs 项目）",
        "数据库题",
        "用了索引",
    ]
    assert history[0].role is TurnRole.INTERVIEWER
    assert history[2].role is TurnRole.INTERVIEWER
    assert history[3].role is TurnRole.CANDIDATE


def test_english_summary() -> None:
    plan = make_plan()
    turns = [
        record(TurnRole.INTERVIEWER, "深挖 Ledger 项目", "q1", Phase.RESUME_DEEP_DIVE),
        record(TurnRole.INTERVIEWER, "深挖 Northwind Labs 项目", "q2", Phase.RESUME_DEEP_DIVE),
    ]
    history = build_history(turns, plan, Phase.TECHNICAL, language=Language.EN)
    assert history[0].text == (
        "(Completed: resume deep dive, asked 2 questions: "
        "深挖 Ledger 项目, 深挖 Northwind Labs 项目)"
    )


def test_current_turn_is_truncated() -> None:
    plan = make_plan()
    turns = [record(TurnRole.CANDIDATE, "x" * 500, phase=Phase.TECHNICAL)]
    history = build_history(turns, plan, Phase.TECHNICAL, language=Language.ZH)
    assert len(history) == 1
    assert history[0].text == "x" * MAX_TURN_CHARS
    assert MAX_TURN_CHARS == 400


def test_history_limit_drops_earliest_current_turns() -> None:
    plan = make_plan()
    turns = [record(TurnRole.INTERVIEWER, "自我介绍", phase=Phase.SELF_INTRO)]
    turns += [
        record(TurnRole.CANDIDATE, f"t{index}", phase=Phase.TECHNICAL) for index in range(1, 21)
    ]
    history = build_history(turns, plan, Phase.TECHNICAL, language=Language.ZH)
    assert len(history) == MAX_HISTORY_TURNS
    assert history[0].text == "（已完成：自我介绍阶段）"
    assert history[1].text == "t10"
    assert history[-1].text == "t20"


def test_earlier_phase_without_questions_still_summarized() -> None:
    plan = make_plan()
    turns = [record(TurnRole.CANDIDATE, "我是 Alex", phase=Phase.SELF_INTRO)]
    history = build_history(turns, plan, Phase.RESUME_DEEP_DIVE, language=Language.ZH)
    assert history == [HistoryTurn(role=TurnRole.INTERVIEWER, text="（已完成：自我介绍阶段）")]


def test_build_history_infers_missing_phases() -> None:
    plan = make_plan()
    turns = [
        record(TurnRole.INTERVIEWER, "深挖 Ledger 项目", "q1"),
        record(TurnRole.CANDIDATE, "我做了 Ledger", "q1"),
    ]
    history = build_history(turns, plan, Phase.RESUME_DEEP_DIVE, language=Language.ZH)
    assert [item.text for item in history] == ["深挖 Ledger 项目", "我做了 Ledger"]


def test_infer_phases_advances_and_never_backtracks() -> None:
    plan = make_plan()
    turns = [
        record(TurnRole.INTERVIEWER, "开场", None),
        record(TurnRole.INTERVIEWER, "深挖 q1", "q1"),
        record(TurnRole.CANDIDATE, "回答 q1", "q1"),
        record(TurnRole.INTERVIEWER, "技术 q3", "q3"),
        record(TurnRole.CANDIDATE, "回答 q3", "q3"),
        record(TurnRole.INTERVIEWER, "又回到 q1", "q1"),
        record(TurnRole.CANDIDATE, "无题", None),
        record(TurnRole.CANDIDATE, "未知题", "missing"),
    ]
    phases = [item.phase for item in infer_phases(turns, plan)]
    assert phases == [
        Phase.OPENING,
        Phase.RESUME_DEEP_DIVE,
        Phase.RESUME_DEEP_DIVE,
        Phase.TECHNICAL,
        Phase.TECHNICAL,
        Phase.TECHNICAL,
        Phase.TECHNICAL,
        Phase.TECHNICAL,
    ]


def test_infer_phases_first_turn_without_question_is_opening() -> None:
    plan = make_plan()
    turns = [record(TurnRole.INTERVIEWER, "开场", None)]
    (inferred,) = infer_phases(turns, plan)
    assert inferred.phase is Phase.OPENING


def test_infer_phases_empty_and_unknown_question() -> None:
    plan = make_plan()
    assert infer_phases([], plan) == []
    turns = [record(TurnRole.CANDIDATE, "未知题", "missing")]
    (inferred,) = infer_phases(turns, plan)
    assert inferred.phase is Phase.OPENING
