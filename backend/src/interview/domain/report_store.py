"""报告的用户隔离仓储；模型调用前后使用短会话。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.db.models import Evaluation, EvaluationPhase, Report, ReportStatus, Turn, TurnRole
from interview.db.repository import UserScopedRepository
from interview.db.scope import bind_user
from interview.domain.evaluator import TranscriptTurn
from interview.domain.orchestrator_store import InterviewNotFound, TurnRepository, plan_from_row
from interview.domain.plan import InterviewPlan, PlanRepository
from interview.domain.report_models import QuestionReviewResult, ReportBody
from interview.observability.tracing import span


class ReportRepository(UserScopedRepository[Report]):
    model = Report

    async def pending(self, interview_id: uuid.UUID) -> Report:
        row = await self.get(interview_id)
        if row is None:
            row = self.add(Report(interview_id=interview_id, user_id=self.user_id))
        row.status = ReportStatus.PENDING
        row.summary_json = row.generated_at = row.model = row.prompt_version = None
        row.updated_at = datetime.now(UTC)
        return row


class ReportStore:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        user_id: uuid.UUID,
        interview_id: uuid.UUID,
    ) -> None:
        self.sm, self.user_id, self.interview_id = sessionmaker, user_id, interview_id

    async def pending(self) -> None:
        with span("report.store.pending", interview_id=str(self.interview_id), role="report"):
            async with self.sm() as session:
                await ReportRepository(session, self.user_id).pending(self.interview_id)
                await session.commit()

    async def load(self) -> tuple[InterviewPlan, dict[str, list[TranscriptTurn]]]:
        with span("report.store.load", interview_id=str(self.interview_id), role="report"):
            async with self.sm() as session:
                row = await PlanRepository(session, self.user_id).get(self.interview_id)
                if row is None:
                    raise InterviewNotFound("面试计划不存在")
                plan = plan_from_row(row)
                rows = await session.scalars(
                    TurnRepository(session, self.user_id)
                    .select()
                    .where(Turn.interview_id == self.interview_id)
                    .order_by(Turn.seq)
                )
                grouped: dict[str, list[TranscriptTurn]] = {}
                for turn in rows:
                    if turn.question_id is not None:
                        grouped.setdefault(turn.question_id, []).append(
                            TranscriptTurn(
                                turn_id=turn.id,
                                role=turn.role,
                                text=turn.text,
                                start_ms=turn.start_ms,
                                end_ms=turn.end_ms,
                            )
                        )
        return plan, grouped

    async def evaluations(
        self,
        results: list[QuestionReviewResult],
        turns: dict[str, list[TranscriptTurn]],
    ) -> None:
        with span("report.store.evaluations", interview_id=str(self.interview_id), role="report"):
            async with self.sm() as session:
                # 下面的批量 DELETE 也要经过 user_id 拦截，先给会话绑定用户
                bind_user(session.sync_session, self.user_id)
                await session.execute(
                    delete(Evaluation).where(
                        Evaluation.user_id == self.user_id,
                        Evaluation.interview_id == self.interview_id,
                        Evaluation.phase == EvaluationPhase.FINAL,
                    )
                )
                for result in results:
                    candidate = next(
                        t
                        for t in reversed(turns[result.question_id])
                        if t.role is TurnRole.CANDIDATE
                    )
                    session.add(
                        Evaluation(
                            interview_id=self.interview_id,
                            user_id=self.user_id,
                            phase=EvaluationPhase.FINAL,
                            turn_id=candidate.turn_id,
                            question_id=result.question_id,
                            competency=result.competency,
                            score=result.score,
                            evidence_json=[
                                item.model_dump(mode="json") for item in result.evidence
                            ],
                            detail_json=result.model_dump(
                                mode="json",
                                include={
                                    "summary",
                                    "strengths",
                                    "missing_points",
                                    "improved_answer",
                                    "latency_ms",
                                    "retries",
                                    "evidence_dropped",
                                    "evidence_retried",
                                },
                            ),
                            next_action=None,
                            model=f"{result.provider}/{result.model}",
                            prompt_version=result.prompt_version,
                        )
                    )
                await session.commit()

    async def ready(self, body: ReportBody, *, model: str, prompt_version: str) -> None:
        with span("report.store.ready", interview_id=str(self.interview_id), role="report"):
            async with self.sm() as session:
                row = await ReportRepository(session, self.user_id).get(self.interview_id)
                if row is None:
                    raise InterviewNotFound("报告不存在")
                row.status, row.summary_json = ReportStatus.READY, body.model_dump(mode="json")
                row.model, row.prompt_version = model, prompt_version
                await session.flush()
                # SQL 表达式通过 UPDATE 写入，避免给 datetime 字段赋不兼容的静态类型。
                await session.execute(
                    update(Report)
                    .where(Report.interview_id == self.interview_id)
                    .values(generated_at=func.now())
                )
                await session.commit()

    async def failed(self, error: str) -> None:
        with span("report.store.failed", interview_id=str(self.interview_id), role="report"):
            async with self.sm() as session:
                repo = ReportRepository(session, self.user_id)
                row = await repo.get(self.interview_id)
                if row is None:
                    row = await repo.pending(self.interview_id)
                row.status = ReportStatus.FAILED
                row.summary_json, row.generated_at = {"error": error}, None
                await session.commit()
