"""评测结束后从数据库读出一场面试的数据，并计算不需要大模型的指标。"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.db.models import (
    Evaluation,
    EvaluationPhase,
    NextAction,
    ReportStatus,
    Turn,
    TurnRole,
)
from interview.domain.orchestrator_store import (
    EvaluationRepository,
    TurnRepository,
    plan_from_row,
)
from interview.domain.plan import InterviewPlan, InterviewRepository, PlanRepository
from interview.domain.report import ReportBody
from interview.domain.report_store import ReportRepository

SUMMARY_REQUEST = re.compile(
    r"总结|简短|简要|简单说|长话短说|挑重点|说重点|一两句|summari[sz]e|briefly|in short",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TurnView:
    id: uuid.UUID
    role: TurnRole
    text: str
    question_id: str | None


@dataclass
class SessionData:
    plan: InterviewPlan
    jd_text: str
    turns: list[TurnView]
    # 候选人发言 → 实时评估的结论和耗时（毫秒）。超过等待上限的结果编排器没用上，但照样落库
    live_evals: dict[uuid.UUID, tuple[NextAction | None, int | None]] = field(
        default_factory=dict[uuid.UUID, tuple[NextAction | None, int | None]]
    )
    final_evals: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    report: ReportBody | None = None


async def load_session(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID, interview_id: uuid.UUID
) -> SessionData:
    async with sm() as session:
        interview = await InterviewRepository(session, user_id).get(interview_id)
        plan_row = await PlanRepository(session, user_id).get(interview_id)
        if interview is None or plan_row is None:
            raise LookupError(f"评测面试 {interview_id} 不存在")
        turns = await session.scalars(
            TurnRepository(session, user_id)
            .select()
            .where(Turn.interview_id == interview_id)
            .order_by(Turn.seq)
        )
        evals = list(
            await session.scalars(
                EvaluationRepository(session, user_id)
                .select()
                .where(Evaluation.interview_id == interview_id)
            )
        )
        report_row = await ReportRepository(session, user_id).get(interview_id)
        return SessionData(
            plan=plan_from_row(plan_row),
            jd_text=interview.jd_text,
            turns=[TurnView(t.id, t.role, t.text, t.question_id) for t in turns],
            live_evals={
                # 有提前决策时，编排器等的是 decision_ms，不是完整评估的 latency_ms
                e.turn_id: (
                    e.next_action,
                    _int(e.detail_json.get("decision_ms")) or _int(e.detail_json.get("latency_ms")),
                )
                for e in evals
                if e.phase is EvaluationPhase.LIVE and e.turn_id is not None
            },
            final_evals=[
                {"evidence": len(e.evidence_json), **e.detail_json}
                for e in evals
                if e.phase is EvaluationPhase.FINAL
            ],
            report=ReportBody.model_validate(report_row.summary_json)
            if report_row is not None and report_row.status is ReportStatus.READY
            else None,
        )


def _int(value: object) -> int | None:
    return int(value) if isinstance(value, int | float | str) and str(value).isdigit() else None


@dataclass(frozen=True)
class FollowUp:
    id: int
    question: str
    key_points: list[str]
    answer: str
    follow_up: str


def follow_ups(data: SessionData) -> list[FollowUp]:
    """候选人答完后，面试官仍停在同一道题上说的话就是追问。

    不能按实时评估的结论判断：评估超过等待上限时编排器已按计划换题，晚到的“追问”结论照样落库。
    """
    questions = {q.id: q for q in data.plan.questions}
    items: list[FollowUp] = []
    for previous, turn in zip(data.turns, data.turns[1:], strict=False):
        if (
            previous.role is TurnRole.CANDIDATE
            and turn.role is TurnRole.INTERVIEWER
            and previous.question_id in questions
            and turn.question_id == previous.question_id
        ):
            question = questions[previous.question_id]
            items.append(
                FollowUp(
                    len(items) + 1, question.text, question.key_points, previous.text, turn.text
                )
            )
    return items


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


def evidence_check(data: SessionData) -> tuple[int, int]:
    """报告里的引用有多少条能在对应发言原文里逐字找到：返回 (引用数, 找到数)。"""
    if data.report is None:
        return 0, 0
    texts = {t.id: _normalize(t.text) for t in data.turns}
    quotes = [quote for q in data.report.questions for quote in q.evidence]
    found = sum(1 for q in quotes if _normalize(q.quote) in texts.get(q.turn_id, ""))
    return len(quotes), found


def raw_evidence(data: SessionData) -> tuple[int, int]:
    """报告评分时模型给出的引用：返回 (总数, 被代码丢掉的数)，用来看模型的原始幻觉率。"""
    kept = sum(int(e.get("evidence", 0)) for e in data.final_evals)
    dropped = sum(int(e.get("evidence_dropped", 0)) for e in data.final_evals)
    return kept + dropped, dropped


def missed_follow_ups(data: SessionData) -> int:
    """实时评估想追问、但面试官已经换题的次数（评估太慢被降级的直接后果）。"""
    missed = 0
    for previous, turn in zip(data.turns, data.turns[1:], strict=False):
        action, _ = data.live_evals.get(previous.id, (None, None))
        if (
            previous.role is TurnRole.CANDIDATE
            and action is NextAction.FOLLOW_UP
            and turn.role is TurnRole.INTERVIEWER
            and turn.question_id != previous.question_id
        ):
            missed += 1
    return missed


def live_latencies(data: SessionData) -> list[int]:
    return sorted(ms for _, ms in data.live_evals.values() if ms is not None)


def asked_to_summarize(data: SessionData) -> bool:
    return any(
        t.role is TurnRole.INTERVIEWER and SUMMARY_REQUEST.search(t.text) for t in data.turns
    )
