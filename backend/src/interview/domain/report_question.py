"""报告的单题最终复评，复用实时评估器的原话证据校验。"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence

from interview.config import ReportSettings
from interview.db.models import TurnRole, UsageRole
from interview.domain.evaluator import TranscriptTurn, validate_evidence
from interview.domain.plan import PlanCompetency, Question
from interview.domain.report_models import QuestionReviewDraft, QuestionReviewResult
from interview.observability.tracing import span
from interview.prompts.loader import get_prompt_library
from interview.prompts.structured import StructuredResult, generate_structured
from interview.providers.base import LLMProvider
from interview.providers.observed import marked_retry


async def review_question(
    llm: LLMProvider,
    question: Question,
    competency: PlanCompetency,
    turns: Sequence[TranscriptTurn],
    *,
    interview_id: uuid.UUID,
    settings: ReportSettings,
) -> QuestionReviewResult:
    started = time.perf_counter()
    question_data = "\n".join(
        [
            f"题目：{question.text}",
            f"能力项：{competency.name}",
            f"能力项描述：{competency.description}",
            "好回答应包含的要点：",
            *(f"- {point}" for point in question.key_points),
        ]
    )
    labels = {TurnRole.INTERVIEWER: "面试官", TurnRole.CANDIDATE: "候选人"}
    transcript = "\n".join(f"{turn.turn_id} | {labels[turn.role]} | {turn.text}" for turn in turns)

    async def generate(evidence_feedback: bool) -> StructuredResult[QuestionReviewDraft]:
        prompt = get_prompt_library().render(
            "report_question",
            question_data=question_data,
            transcript=transcript,
            evidence_feedback=evidence_feedback,
        )
        return await generate_structured(
            llm,
            prompt,
            QuestionReviewDraft,
            max_tokens=4096,
            timeout_s=settings.question_timeout_s,
        )

    with span(
        "report.question", interview_id=str(interview_id), role=UsageRole.REPORT.value
    ) as current:
        current.set_attribute("question.id", question.id)
        used = await generate(False)
        retries = used.retries
        kept, dropped = validate_evidence(used.value.evidence, turns)
        evidence_retried = used.value.score is not None and not kept
        if evidence_retried:
            with marked_retry():
                used = await generate(True)
            retries += used.retries + 1
            kept, more_dropped = validate_evidence(used.value.evidence, turns)
            dropped += more_dropped
        draft = used.value
        score_nulled = not kept and (draft.score is not None or evidence_retried)
        current.set_attributes(
            {
                "report.evidence_kept": len(kept),
                "report.evidence_dropped": dropped,
                "report.score_nulled": score_nulled,
                "report.retries": retries,
            }
        )
        return QuestionReviewResult(
            question_id=question.id,
            question=question.text,
            competency=competency.name,
            asked=True,
            score=draft.score if kept else None,
            evidence=kept,
            summary=draft.summary,
            strengths=draft.strengths,
            missing_points=draft.missing_points,
            improved_answer=draft.improved_answer,
            provider=used.provider,
            model=used.model,
            prompt_version=f"{used.prompt_id}@{used.prompt_version}",
            latency_ms=int((time.perf_counter() - started) * 1000),
            retries=retries,
            evidence_dropped=dropped,
            evidence_retried=evidence_retried,
        )
