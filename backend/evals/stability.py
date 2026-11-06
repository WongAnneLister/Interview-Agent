"""稳定性：同一份转写把报告的逐题评分重复跑 N 次，看分数波动（总体标准差）。"""

from __future__ import annotations

import asyncio
import statistics
import uuid
from dataclasses import dataclass, field

from evals.session import EvalContext
from interview.db.models import TurnRole, UsageRole
from interview.domain.report import competency_reviews, overall_score
from interview.domain.report_models import QuestionReview
from interview.domain.report_question import review_question
from interview.domain.report_store import ReportStore
from interview.observability.tracing import span
from interview.providers.observed import eval_mode

REPEATS = 5


@dataclass
class Stability:
    interview_id: uuid.UUID
    repeats: int
    question_scores: dict[str, list[int]] = field(default_factory=dict[str, list[int]])
    overall_scores: list[float] = field(default_factory=list[float])
    failures: int = 0

    @property
    def question_std(self) -> list[float]:
        return [statistics.pstdev(s) for s in self.question_scores.values() if len(s) >= 2]

    @property
    def overall_std(self) -> float | None:
        return statistics.pstdev(self.overall_scores) if len(self.overall_scores) >= 2 else None


async def measure(ec: EvalContext, interview_id: uuid.UUID, repeats: int = REPEATS) -> Stability:
    result = Stability(interview_id, repeats)
    store = ReportStore(ec.sessionmaker, ec.user_id, interview_id)
    semaphore = asyncio.Semaphore(ec.report.concurrency)
    with eval_mode(), span("eval.stability", interview_id=str(interview_id), is_eval=True):
        plan, turns = await store.load()
        competencies = {c.name: c for c in plan.competencies}
        asked = [
            q
            for q in plan.questions
            if q.competency in competencies
            and any(t.role is TurnRole.CANDIDATE for t in turns.get(q.id, []))
        ]

        async def one(round_no: int) -> list[QuestionReview]:
            async def review(question_id: str) -> QuestionReview:
                question = next(q for q in asked if q.id == question_id)
                llm = ec.providers.report_llm(ec.ctx(UsageRole.REPORT, interview_id))
                async with semaphore:
                    try:
                        reviewed = await review_question(
                            llm,
                            question,
                            competencies[question.competency],
                            turns[question.id],
                            interview_id=interview_id,
                            settings=ec.report,
                        )
                    except Exception:
                        result.failures += 1
                        return QuestionReview(
                            question_id=question.id,
                            question=question.text,
                            competency=question.competency,
                            asked=True,
                        )
                return QuestionReview.model_validate(reviewed.model_dump())

            return list(await asyncio.gather(*(review(q.id) for q in asked)))

        rounds = await asyncio.gather(*(one(i) for i in range(repeats)))
    for reviews in rounds:
        for review in reviews:
            if review.score is not None:
                result.question_scores.setdefault(review.question_id, []).append(review.score)
        overall = overall_score(competency_reviews(plan, reviews))
        if overall is not None:
            result.overall_scores.append(overall)
    return result
