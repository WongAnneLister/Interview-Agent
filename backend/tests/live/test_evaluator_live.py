"""真实调用 llm_eval 连续评估一段固定问答（make test-live）。

打印完整耗时的 P50 / P95、每次 retries 和 evidence_retried，不对耗时做断言。
未配置 llm_eval 时跳过；用量写入用一个丢弃用量的 stub 会话，避免 live 测试依赖数据库。
"""

from __future__ import annotations

import math
import uuid
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session
from test_llm_live import provider_settings

from interview.db.models import NextAction, TurnRole, UsageRole
from interview.domain.evaluator import (
    DraftEvidence,
    EvaluationInput,
    EvaluationResult,
    TranscriptTurn,
    evaluate,
    validate_evidence,
)
from interview.domain.plan import PlanCompetency, Question, Stage
from interview.providers.observed import CallContext
from interview.providers.registry import ProviderNotConfigured, ProviderRegistry

INTERVIEW_ID = uuid.UUID("00000000-0000-4000-8000-000000000021")
INTERVIEWER_ID = uuid.UUID("00000000-0000-4000-8000-000000000022")
CANDIDATE_ID = uuid.UUID("00000000-0000-4000-8000-000000000023")

QUESTION = Question(
    id="q1",
    stage=Stage.TECHNICAL,
    competency="系统设计",
    text="如果消息队列出现大量积压，你会怎么定位和处理？",
    key_points=["监控积压量", "定位消费瓶颈", "扩容或限流", "补偿与重试"],
    follow_ups=["如何保证顺序", "如何避免重复消费"],
)
COMPETENCY = PlanCompetency(
    name="系统设计",
    description="分布式系统与消息队列设计能力",
    weight=1.0,
    probes=["积压处理"],
    covered=True,
)
TURNS = [
    TranscriptTurn(
        turn_id=INTERVIEWER_ID,
        role=TurnRole.INTERVIEWER,
        text="如果消息队列出现大量积压，你会怎么定位和处理？",
        start_ms=0,
        end_ms=2_000,
    ),
    TranscriptTurn(
        turn_id=CANDIDATE_ID,
        role=TurnRole.CANDIDATE,
        text=(
            "我先看消费端的处理耗时和积压曲线，确认是流量突增还是消费变慢。"
            "如果是下游数据库慢，就先限流并扩容消费者；同时把失败消息放进重试队列，"
            "记录失败原因，避免一直阻塞主队列。"
        ),
        start_ms=2_000,
        end_ms=32_000,
    ),
]
INPUT = EvaluationInput(
    question=QUESTION,
    competency=COMPETENCY,
    turns=TURNS,
    follow_ups=0,
    max_follow_ups=2,
    stage_remaining_ms=180_000,
)


class DiscardSession:
    def __init__(self) -> None:
        self.sync_session = Session()

    async def __aenter__(self) -> DiscardSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def add_all(self, _events: list[Any]) -> None:
        return None

    async def commit(self) -> None:
        return None


class DiscardSessionmaker:
    def __call__(self) -> DiscardSession:
        return DiscardSession()


def percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


@pytest.mark.live
async def test_evaluate_fixed_answer(capsys: pytest.CaptureFixture[str]) -> None:
    settings = provider_settings()
    if not settings.llm_eval.configured:
        pytest.skip("LLM_EVAL_PROVIDER / LLM_EVAL_MODEL 未配置")
    registry = ProviderRegistry(
        settings,
        sessionmaker=cast(Any, DiscardSessionmaker()),
    )
    try:
        try:
            llm = registry.llm(
                "llm_eval",
                CallContext(
                    user_id=uuid.uuid4(),
                    interview_id=INTERVIEW_ID,
                    usage_role=UsageRole.EVAL,
                ),
            )
        except ProviderNotConfigured as exc:
            pytest.skip(str(exc))
        results: list[EvaluationResult] = []
        for _ in range(15):
            result = await evaluate(llm, INPUT, interview_id=INTERVIEW_ID)
            assert result.prompt_version == "evaluate_answer@1"
            assert result.next_action in NextAction
            assert result.score is None or 1 <= result.score <= 5
            kept, dropped = validate_evidence(
                [
                    DraftEvidence(turn_id=quote.turn_id, quote=quote.quote)
                    for quote in result.evidence
                ],
                TURNS,
            )
            assert len(kept) == len(result.evidence) and dropped == 0
            results.append(result)
    finally:
        await registry.aclose()

    latencies = [result.latency_ms for result in results]
    with capsys.disabled():
        print(f"\n[evaluator] {settings.llm_eval.provider}/{settings.llm_eval.model}，15 次")
        for index, result in enumerate(results, start=1):
            print(
                f"  #{index:02d} {result.latency_ms} ms  retries={result.retries}  "
                f"evidence_retried={result.evidence_retried}  score={result.score}  "
                f"evidence={len(result.evidence)}"
            )
        print(f"  P50={percentile(latencies, 0.50)} ms  P95={percentile(latencies, 0.95)} ms")
