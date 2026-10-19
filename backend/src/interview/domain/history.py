"""对话历史压缩：当前阶段原样保留，更早阶段每个压成一条代码生成的摘要。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from interview.db.models import Language, TurnRole
from interview.domain.interviewer import MAX_HISTORY_TURNS, HistoryTurn
from interview.domain.plan import InterviewPlan
from interview.domain.state_machine import Phase

MAX_TURN_CHARS = 400

_PHASE_LABELS: Mapping[Language, Mapping[Phase, str]] = {
    Language.ZH: {
        Phase.OPENING: "开场",
        Phase.SELF_INTRO: "自我介绍",
        Phase.RESUME_DEEP_DIVE: "简历深挖",
        Phase.TECHNICAL: "技术问题",
        Phase.BEHAVIORAL: "行为问题",
        Phase.CANDIDATE_QUESTIONS: "候选人提问",
        Phase.CLOSING: "收尾",
        Phase.ENDED: "结束",
    },
    Language.EN: {
        Phase.OPENING: "opening",
        Phase.SELF_INTRO: "self introduction",
        Phase.RESUME_DEEP_DIVE: "resume deep dive",
        Phase.TECHNICAL: "technical questions",
        Phase.BEHAVIORAL: "behavioral questions",
        Phase.CANDIDATE_QUESTIONS: "candidate questions",
        Phase.CLOSING: "closing",
        Phase.ENDED: "ended",
    },
}


class TurnRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: TurnRole
    text: str
    question_id: str | None = None
    phase: Phase | None = None


def _phase_order(plan: InterviewPlan) -> list[Phase]:
    return [*(Phase(slot.stage.value) for slot in plan.stages), Phase.ENDED]


def _summary(phase: Phase, question_texts: Sequence[str], language: Language) -> str:
    label = _PHASE_LABELS[language][phase]
    if language == Language.ZH:
        if question_texts:
            joined = "、".join(question_texts)
            return f"（已完成：{label}阶段，问了 {len(question_texts)} 道题：{joined}）"
        return f"（已完成：{label}阶段）"
    if question_texts:
        return (
            f"(Completed: {label}, asked {len(question_texts)} questions: "
            f"{', '.join(question_texts)})"
        )
    return f"(Completed: {label})"


def infer_phases(turns: Sequence[TurnRecord], plan: InterviewPlan) -> list[TurnRecord]:
    phase_by_question = {question.id: Phase(question.stage.value) for question in plan.questions}
    order = _phase_order(plan)
    indices = {phase: index for index, phase in enumerate(order)}
    current = Phase.OPENING
    current_index = indices.get(current, 0)
    result: list[TurnRecord] = []
    for turn in turns:
        candidate = phase_by_question.get(turn.question_id) if turn.question_id else current
        if candidate is None:
            candidate = current
        candidate_index = indices.get(candidate, current_index)
        if candidate_index < current_index:
            candidate = current
            candidate_index = current_index
        current = candidate
        current_index = candidate_index
        result.append(turn.model_copy(update={"phase": current}))
    return result


def build_history(
    turns: Sequence[TurnRecord],
    plan: InterviewPlan,
    current_phase: Phase,
    *,
    language: Language,
) -> list[HistoryTurn]:
    records = list(turns)
    if any(record.phase is None for record in records):
        records = infer_phases(records, plan)
    order = _phase_order(plan)
    current_index = order.index(current_phase) if current_phase in order else len(order) - 1
    summaries: list[HistoryTurn] = []
    for phase in order[:current_index]:
        phase_turns = [record for record in records if record.phase == phase]
        if not phase_turns:
            continue
        asked = {record.question_id for record in phase_turns if record.question_id}
        question_texts = [
            question.text[:20]
            for question in plan.questions
            if question.stage.value == phase.value and question.id in asked
        ]
        summaries.append(
            HistoryTurn(
                role=TurnRole.INTERVIEWER,
                text=_summary(phase, question_texts, language),
            )
        )
    current = [
        HistoryTurn(role=record.role, text=record.text[:MAX_TURN_CHARS])
        for record in records
        if record.phase == current_phase
    ]
    allowed = max(0, MAX_HISTORY_TURNS - len(summaries))
    return [*summaries, *(current[-allowed:] if allowed else [])]
