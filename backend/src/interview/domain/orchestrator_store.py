"""编排器的数据库读写：加载一场面试，写 turn、状态和实时评估。

每次读写都只开一个短会话；调用模型期间不占会话。写入用一把锁串行化：
评估落库是后台任务，会和主流程的 turn / 状态写入同时发生。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.db.models import (
    Evaluation,
    EvaluationPhase,
    InterviewStatus,
    InterviewStyle,
    Language,
    Plan,
    Turn,
    TurnRole,
)
from interview.db.repository import UserScopedRepository
from interview.domain.evaluator import EvaluationResult, TranscriptTurn
from interview.domain.history import TurnRecord, infer_phases
from interview.domain.plan import (
    InterviewPlan,
    InterviewRepository,
    PlanCompetency,
    PlanRepository,
    Question,
    StageSlot,
)
from interview.domain.state_machine import InterviewState


class TurnRepository(UserScopedRepository[Turn]):
    model = Turn


class EvaluationRepository(UserScopedRepository[Evaluation]):
    model = Evaluation


class InterviewNotFound(LookupError):
    """面试或面试计划不存在（含不属于这个用户）。"""


def plan_from_row(row: Plan) -> InterviewPlan:
    return InterviewPlan(
        stages=[StageSlot.model_validate(item) for item in row.stages_json],
        competencies=[PlanCompetency.model_validate(item) for item in row.competencies_json],
        questions=[Question.model_validate(item) for item in row.questions_json],
        tech_terms=list(row.tech_terms_json),
        prompt_version=row.prompt_version,
    )


@dataclass
class LoadedInterview:
    """从数据库读出的一场面试，供编排器恢复内存状态。state 为空表示还没开始。"""

    plan: InterviewPlan
    style: InterviewStyle
    language: Language
    state: InterviewState | None
    ended_by_user: bool
    turns: list[TurnRecord] = field(default_factory=list[TurnRecord])
    question_turns: dict[str, list[TranscriptTurn]] = field(
        default_factory=dict[str, list[TranscriptTurn]]
    )
    turn_seq: int = 0


class InterviewStore:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        interview_id: uuid.UUID,
        user_id: uuid.UUID,
        turn_seq: int = 0,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._interview_id = interview_id
        self._user_id = user_id
        self._turn_seq = turn_seq
        self._lock = asyncio.Lock()

    @property
    def turn_seq(self) -> int:
        return self._turn_seq

    async def load(self) -> LoadedInterview:
        async with self._sessionmaker() as session:
            interview = await InterviewRepository(session, self._user_id).get(self._interview_id)
            if interview is None or interview.user_id != self._user_id:
                raise InterviewNotFound("面试不存在")
            plan_row = await PlanRepository(session, self._user_id).get(self._interview_id)
            if plan_row is None:
                raise InterviewNotFound("面试计划不存在")
            rows = list(
                (
                    await session.scalars(
                        TurnRepository(session, self._user_id).select().order_by(Turn.seq)
                    )
                ).all()
            )
            plan = plan_from_row(plan_row)
            loaded = LoadedInterview(
                plan=plan,
                style=interview.style,
                language=interview.language,
                state=None,
                ended_by_user=False,
            )
            raw = interview.state_json
            if isinstance(raw, dict):
                machine = raw.get("machine")
                if machine is not None:
                    loaded.state = InterviewState.model_validate(machine)
                loaded.ended_by_user = bool(raw.get("ended_by_user", False))
        records = [
            TurnRecord(role=row.role, text=row.text, question_id=row.question_id) for row in rows
        ]
        loaded.turns = infer_phases(records, plan)
        for row in rows:
            if row.question_id is not None:
                loaded.question_turns.setdefault(row.question_id, []).append(
                    TranscriptTurn(
                        turn_id=row.id,
                        role=row.role,
                        text=row.text,
                        start_ms=row.start_ms,
                        end_ms=row.end_ms,
                    )
                )
        loaded.turn_seq = max((row.seq for row in rows), default=0)
        self._turn_seq = loaded.turn_seq
        return loaded

    async def append_turn(
        self,
        *,
        role: TurnRole,
        text: str,
        question_id: str | None,
        start_ms: int,
        end_ms: int,
    ) -> uuid.UUID:
        turn_id = uuid.uuid4()
        async with self._lock, self._sessionmaker() as session:
            seq = self._turn_seq + 1  # 在锁内分配，避免两次写入拿到同一个 seq
            TurnRepository(session, self._user_id).add(
                Turn(
                    id=turn_id,
                    interview_id=self._interview_id,
                    user_id=self._user_id,
                    seq=seq,
                    role=role,
                    text=text,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    interrupted=False,
                    question_id=question_id,
                )
            )
            await session.commit()
            self._turn_seq = seq
        return turn_id

    async def save_state(
        self,
        state: InterviewState,
        *,
        ended_by_user: bool,
        status: InterviewStatus | None = None,
        started: bool = False,
        ended: bool = False,
    ) -> None:
        async with self._lock, self._sessionmaker() as session:
            interview = await InterviewRepository(session, self._user_id).get(self._interview_id)
            if interview is None:
                raise InterviewNotFound("面试不存在")
            interview.state_json = {
                "machine": state.model_dump(mode="json"),
                "ended_by_user": ended_by_user,
            }
            if status is not None:
                interview.status = status
            if started:
                interview.started_at = cast(Any, func.now())
            if ended:
                interview.ended_at = cast(Any, func.now())
            await session.commit()

    async def write_evaluation(self, result: EvaluationResult, turn_id: uuid.UUID) -> None:
        detail = {
            "missing_points": result.missing_points,
            "highlights": result.highlights,
            "rationale": result.rationale,
            "follow_up_focus": result.follow_up_focus,
            "override_reason": result.override_reason,
            "latency_ms": result.latency_ms,
            "evidence_dropped": result.evidence_dropped,
            "evidence_retried": result.evidence_retried,
            "retries": result.retries,
        }
        async with self._lock, self._sessionmaker() as session:
            EvaluationRepository(session, self._user_id).add(
                Evaluation(
                    interview_id=self._interview_id,
                    user_id=self._user_id,
                    phase=EvaluationPhase.LIVE,
                    turn_id=turn_id,
                    question_id=result.question_id,
                    competency=result.competency,
                    score=result.score,
                    evidence_json=[item.model_dump(mode="json") for item in result.evidence],
                    detail_json=detail,
                    next_action=result.next_action,
                    model=f"{result.provider}/{result.model}",
                    prompt_version=result.prompt_version,
                )
            )
            await session.commit()
