"""编排器每轮的后台工作：实时评估、评估落库、预算检查，以及“最多等多久”的降级规则。

评估和预算查询在候选人说完后立刻启动，与过渡语并行；等待上限从启动时算起。
评估任务从不取消：超时或出错只影响这一轮的决策，晚到的结果照样落库。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.domain.evaluator import EvaluationInput, EvaluationResult, evaluate
from interview.domain.orchestrator_store import InterviewStore
from interview.observability import budget
from interview.observability.budget import BudgetStatus
from interview.observability.prometheus import counter, histogram
from interview.observability.tracing import OpenSpan
from interview.prompts.structured import StructuredOutputError
from interview.providers.base import LLMProvider, ProviderError

log = structlog.get_logger(__name__)

_EVAL_DEGRADED = counter("orchestrator_eval_degraded", "实时评估降级的次数", ["reason"])
_EVAL_WAIT = histogram(
    "orchestrator_eval_wait_seconds",
    "等待实时评估的时间（秒）",
    buckets=[0.5, 1, 1.5, 2, 2.5, 3, 5],
)

BUDGET_WAIT_S = 0.5

type Action = Literal["follow_up", "next_question", "next_stage", "wrap_up"]


@dataclass(frozen=True)
class TurnDecision:
    action: Action
    focus: str
    degraded: bool
    wait_ms: int


class TurnBackground:
    """一轮候选人发言启动的后台任务。创建即启动；start_ms 用于计算剩余等待时间。"""

    def __init__(
        self,
        *,
        interview_id: uuid.UUID,
        user_id: uuid.UUID,
        sessionmaker: async_sessionmaker[AsyncSession],
        store: InterviewStore,
        track: Callable[[asyncio.Task[Any]], None],
        span: OpenSpan,
        evaluation: tuple[LLMProvider, EvaluationInput, uuid.UUID] | None,
    ) -> None:
        self._interview_id = interview_id
        self._store = store
        self._span = span
        self._started = time.perf_counter()
        self._eval: asyncio.Task[EvaluationResult] | None = None
        with span.activate():
            self._budget = asyncio.create_task(
                budget.check(interview_id, user_id=user_id, sessionmaker=sessionmaker)
            )
            if evaluation is not None:
                llm, data, turn_id = evaluation
                self._eval = asyncio.create_task(evaluate(llm, data, interview_id=interview_id))
                track(asyncio.create_task(self._store_evaluation(self._eval, turn_id)))
        track(self._budget)

    async def decide(self, wait_s: float) -> TurnDecision:
        """等评估结果（从启动时算最多 wait_s 秒）和预算；任何失败都降级为按计划换题。"""
        action: Action = "follow_up"
        focus = ""
        degraded = False
        wait_ms = 0
        if self._eval is not None:
            remaining = wait_s - (time.perf_counter() - self._started)
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(self._eval), timeout=max(0.0, remaining)
                )
                action = result.next_action.value
                focus = result.follow_up_focus
            except (ProviderError, StructuredOutputError) as exc:
                # ProviderTimeout 也是 TimeoutError，所以先接 ProviderError，计为 error
                degraded, action = True, "next_question"
                _EVAL_DEGRADED.labels(reason="error").inc()
                log.warning(
                    "orchestrator_eval_degraded",
                    interview_id=str(self._interview_id),
                    reason="error",
                    error=type(exc).__name__,
                )
            except TimeoutError:
                degraded, action = True, "next_question"
                _EVAL_DEGRADED.labels(reason="timeout").inc()
            elapsed = time.perf_counter() - self._started
            wait_ms = int(elapsed * 1000)
            _EVAL_WAIT.observe(elapsed)
        self._span.span.set_attribute("orchestrator.eval_degraded", degraded)
        self._span.span.set_attribute("orchestrator.eval_wait_ms", wait_ms)
        if await self._budget_status() == BudgetStatus.EXCEEDED:
            action, focus = "wrap_up", ""
        return TurnDecision(action=action, focus=focus, degraded=degraded, wait_ms=wait_ms)

    async def _budget_status(self) -> BudgetStatus:
        # 评估已经等过一轮，预算查询再慢也不能卡住面试；超时按 ok 继续，任务在后台跑完即可
        try:
            result = await asyncio.wait_for(asyncio.shield(self._budget), timeout=BUDGET_WAIT_S)
        except Exception as exc:
            log.warning(
                "orchestrator_budget_check_failed",
                interview_id=str(self._interview_id),
                error=type(exc).__name__,
            )
            self._span.span.set_attribute("budget.status", BudgetStatus.OK.value)
            return BudgetStatus.OK
        self._span.span.set_attribute("budget.status", result.status.value)
        if result.status != BudgetStatus.OK:
            log.warning(
                f"orchestrator_budget_{result.status.value}",
                interview_id=str(self._interview_id),
                spent_cny=str(result.interview.spent_cny),
            )
        return result.status

    async def _store_evaluation(
        self, task: asyncio.Task[EvaluationResult], turn_id: uuid.UUID
    ) -> None:
        try:
            result = await task
        except (ProviderError, StructuredOutputError) as exc:
            log.warning(
                "orchestrator_evaluation_failed",
                interview_id=str(self._interview_id),
                error=type(exc).__name__,
            )
            return
        try:
            await self._store.write_evaluation(result, turn_id)
        except Exception as exc:
            log.warning(
                "orchestrator_evaluation_store_failed",
                interview_id=str(self._interview_id),
                error=type(exc).__name__,
            )
