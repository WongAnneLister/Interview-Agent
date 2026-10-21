"""结束后的后台复评；模型调用并发，数据库读写使用短会话。"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.config import ReportSettings
from interview.db.models import TurnRole, UsageRole
from interview.domain.plan import InterviewPlan, Question
from interview.domain.report_models import (
    REPORT_DISCLAIMER as REPORT_DISCLAIMER,
)
from interview.domain.report_models import (
    CompetencyReview,
    QuestionReview,
    QuestionReviewResult,
    ReportSummaryDraft,
)
from interview.domain.report_models import (
    ReportBody as ReportBody,
)
from interview.domain.report_question import review_question as review_question
from interview.domain.report_store import ReportStore
from interview.observability.prometheus import counter, histogram
from interview.observability.tracing import span
from interview.prompts.loader import get_prompt_library
from interview.prompts.structured import StructuredOutputError, generate_structured
from interview.providers.base import ChatMessage, LLMProvider, LLMResult, ProviderError
from interview.providers.observed import CallContext, marked_retry
from interview.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)
_GENERATED = counter("report_generated", "报告生成结果", ["status"])
_DURATION = histogram(
    "report_duration_seconds", "报告生成耗时", buckets=(5, 10, 20, 30, 45, 60, 90, 120)
)


class _CountedLLM:
    """包一层厂商对象，只为数清一道题的 complete 调用次数。

    包括失败的调用和 generate_structured 内部的格式重试。

    detail_json.retries 要的是“这道题总共多调了几次模型”，StructuredResult.retries 只知道最后一轮。
    报告只用到 complete；其余属性原样转发。
    """

    def __init__(self, inner: LLMProvider) -> None:
        self.inner, self.calls = inner, 0
        self.provider, self.model = inner.provider, inner.model
        self.stream_chat, self.aclose = inner.stream_chat, inner.aclose
        self.last_provider = self.last_model = ""

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> LLMResult:
        self.calls += 1
        result = await self.inner.complete(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_s=timeout_s,
            extra=extra,
        )
        self.last_provider, self.last_model = result.provider, result.model
        return result


@dataclass
class _Progress:
    questions: int = 0
    degraded: int = 0


def _competencies(plan: InterviewPlan, questions: list[QuestionReview]) -> list[CompetencyReview]:
    result: list[CompetencyReview] = []
    for competency in plan.competencies:
        items = [q for q in questions if q.competency == competency.name and q.score is not None]
        scores = [q.score for q in items if q.score is not None]
        score = round(sum(scores) / len(scores), 1) if scores and competency.covered else None
        result.append(
            CompetencyReview(
                name=competency.name,
                weight=competency.weight,
                score=score,
                conclusion="本场未考察到" if score is None else f"得分 {score} 分，详见各题反馈",
                evidence=[quote for q in items for quote in q.evidence]
                if score is not None
                else [],
            )
        )
    return result


async def _generate(
    store: ReportStore,
    providers: ProviderRegistry,
    settings: ReportSettings,
    progress: _Progress,
) -> tuple[int, int]:
    await store.pending()
    plan, turns = await store.load()
    progress.questions = sum(
        any(t.role == TurnRole.CANDIDATE for t in turns.get(q.id, [])) for q in plan.questions
    )
    semaphore = asyncio.Semaphore(settings.concurrency)
    by_name = {c.name: c for c in plan.competencies}
    degraded: list[str] = []
    failures: list[Exception] = []
    library = get_prompt_library()
    question_version = library.get("report_question").meta.version
    summary_version = library.get("report_summary").meta.version

    async def review(question: Question) -> QuestionReviewResult | None:
        question_turns = turns.get(question.id, [])
        candidates = [t for t in question_turns if t.role == TurnRole.CANDIDATE]
        if not candidates:
            return None
        async with semaphore:
            llm = _CountedLLM(
                providers.llm(
                    "llm_eval",
                    CallContext(
                        user_id=store.user_id,
                        interview_id=store.interview_id,
                        turn_id=candidates[-1].turn_id,
                        usage_role=UsageRole.REPORT,
                    ),
                )
            )
            started = time.perf_counter()
            for attempt in range(settings.question_attempts):
                try:
                    with marked_retry(attempt > 0):
                        result = await review_question(
                            llm,
                            question,
                            by_name[question.competency],
                            question_turns,
                            interview_id=store.interview_id,
                            settings=settings,
                        )
                    result.retries = llm.calls - 1
                    result.latency_ms = int((time.perf_counter() - started) * 1000)
                    return result
                except (ProviderError, StructuredOutputError) as exc:
                    # 格式错误在 generate_structured 里已重试过，整题再试只会成倍花钱；
                    # 只有厂商错误（超时、不可用）才整题重试
                    if isinstance(exc, ProviderError) and attempt + 1 < settings.question_attempts:
                        continue
                    failures.append(exc)
                    degraded.append(f"{question.id}: 评分失败")
                    progress.degraded = len(degraded)
                    return QuestionReviewResult(
                        question_id=question.id,
                        question=question.text,
                        competency=question.competency,
                        asked=True,
                        provider=getattr(exc, "provider", "") or llm.last_provider,
                        model=getattr(exc, "model", "") or llm.last_model,
                        prompt_version=f"report_question@{question_version}",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        retries=llm.calls - 1,
                        evidence_dropped=0,
                        evidence_retried=False,
                    )
        return None

    tasks = [asyncio.create_task(review(question)) for question in plan.questions]
    try:
        reviews = await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    results = [r for r in reviews if r is not None]
    await store.evaluations(results, turns)
    if results and len(failures) == len(results):
        raise failures[0]
    questions = [
        QuestionReview.model_validate(result.model_dump())
        if result is not None
        else QuestionReview(question_id=q.id, question=q.text, competency=q.competency, asked=False)
        for q, result in zip(plan.questions, reviews, strict=True)
    ]
    competencies = _competencies(plan, questions)
    scored = [c for c in competencies if c.score is not None]
    weight = sum(c.weight for c in scored)
    overall_score = (
        round(sum((c.score or 0) * c.weight for c in scored) / weight, 1) if weight else None
    )
    overall = "报告汇总暂不可用，请参考各题反馈并优先练习缺失要点。"
    improvements = [
        q.missing_points[0]
        for q in sorted(
            (q for q in questions if q.score is not None and q.missing_points),
            key=lambda q: q.score or 0,
        )
    ][:3]
    model = next((f"{r.provider}/{r.model}" for r in results if r.provider and r.model), "")
    prompt = library.render(
        "report_summary",
        structured_data=json.dumps(
            {
                "competencies": [
                    {
                        "name": c.name,
                        "description": source.description,
                        "weight": c.weight,
                        "score": c.score if c.score is not None else "未考察到",
                    }
                    for c, source in zip(competencies, plan.competencies, strict=True)
                ],
                "questions": [
                    q.model_dump(
                        include={"question", "competency", "score", "summary", "missing_points"}
                    )
                    for q in questions
                ],
            },
            ensure_ascii=False,
        ),
    )
    try:
        llm = providers.llm(
            "llm_eval",
            CallContext(
                user_id=store.user_id,
                interview_id=store.interview_id,
                usage_role=UsageRole.REPORT,
            ),
        )
        with span("report.summary", interview_id=str(store.interview_id), role="report"):
            summary = await generate_structured(
                llm,
                prompt,
                ReportSummaryDraft,
                max_tokens=4096,
                timeout_s=settings.question_timeout_s,
                context={"expected_names": [c.name for c in competencies]},
            )
        conclusions = {c.name: c.conclusion for c in summary.value.competencies}
        for c in competencies:
            c.conclusion = conclusions[c.name] if c.score is not None else "本场未考察到"
        overall, improvements = summary.value.overall, summary.value.top_improvements
        model = f"{summary.provider}/{summary.model}"
    except (ProviderError, StructuredOutputError):
        degraded.append("汇总失败，使用各题反馈")
        progress.degraded = len(degraded)
    body = ReportBody(
        overall_score=overall_score,
        overall=overall,
        top_improvements=improvements,
        competencies=competencies,
        questions=questions,
        degraded=sorted(degraded),
    )
    await store.ready(
        body,
        model=model,
        prompt_version=(f"report_question@{question_version}+report_summary@{summary_version}"),
    )
    return len(results), len(degraded)


async def generate_report(
    *,
    interview_id: uuid.UUID,
    user_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
    providers: ProviderRegistry,
    settings: ReportSettings,
) -> None:
    started = time.perf_counter()
    store = ReportStore(sessionmaker, user_id, interview_id)
    status, progress = "failed", _Progress()
    with span("report.generate", interview_id=str(interview_id), role="report") as current:
        try:
            async with asyncio.timeout(settings.total_timeout_s):
                await _generate(store, providers, settings, progress)
            status = "ready"
        except (Exception, asyncio.CancelledError) as exc:
            error = type(exc).__name__
            try:
                await store.failed(error)
            except Exception as write_error:
                log.error(
                    "report_failed_write",
                    interview_id=str(interview_id),
                    error=type(write_error).__name__,
                )
            log.error(
                "report_failed",
                interview_id=str(interview_id),
                error=error,
                questions=progress.questions,
                degraded=progress.degraded,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            duration = time.perf_counter() - started
            _GENERATED.labels(status=status).inc()
            _DURATION.observe(duration)
            current.set_attributes(
                {
                    "report.questions": progress.questions,
                    "report.degraded": progress.degraded,
                    "report.duration_ms": int(duration * 1000),
                }
            )
        if status == "ready":
            log.info(
                "report_ready",
                interview_id=str(interview_id),
                questions=progress.questions,
                degraded=progress.degraded,
                duration_ms=int(duration * 1000),
            )


class ReportRunner:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        providers: ProviderRegistry,
        settings: ReportSettings,
    ) -> None:
        self.sessionmaker, self.providers, self.settings = sessionmaker, providers, settings
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._closed = False

    def is_running(self, interview_id: uuid.UUID) -> bool:
        task = self._tasks.get(interview_id)
        return task is not None and not task.done()

    def start(self, interview_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        if self._closed or self.is_running(interview_id):
            return False
        task = asyncio.create_task(
            generate_report(
                interview_id=interview_id,
                user_id=user_id,
                sessionmaker=self.sessionmaker,
                providers=self.providers,
                settings=self.settings,
            )
        )
        self._tasks[interview_id] = task

        def finished(done: asyncio.Task[None]) -> None:
            if self._tasks.get(interview_id) is done:
                self._tasks.pop(interview_id)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return True

    async def aclose(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.settings.total_timeout_s)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def request_report(
    interview_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    runner: ReportRunner | None,
) -> None:
    """面试结束后调用：先写 pending 行，再登记后台任务。

    WebSocket 推送 ended 前会 await 这里；先落库，前端收到 ended 立刻查报告
    也能拿到 pending，而不是 404。
    """
    if runner is None:
        log.warning("report_runner_unavailable", interview_id=str(interview_id))
        return
    if runner.is_running(interview_id):
        return
    await ReportStore(runner.sessionmaker, user_id, interview_id).pending()
    runner.start(interview_id, user_id)
