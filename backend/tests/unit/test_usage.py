"""用量记录：token 拆分、写库、span 上的用量与成本、计数器、缺价格与写库失败不中断。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from decimal import Decimal
from types import TracebackType
from typing import Any, cast

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import format_trace_id
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from interview.config import ObservabilitySettings
from interview.db.models import UsageEvent, UsageRole, UsageUnit
from interview.db.scope import bound_user
from interview.db.session import create_engine, make_sessionmaker
from interview.observability.pricing import PriceEntry, PriceTable, UnitPrice
from interview.observability.tracing import LF, init_tracing, llm_span, shutdown_tracing
from interview.observability.usage import llm_quantities, record_usage

USER = uuid.uuid4()
M = 1_000_000


def prices(model: str) -> PriceTable:
    def per_million(value: str) -> UnitPrice:
        return UnitPrice(price_cny=Decimal(value), per=M)

    return PriceTable(
        [
            PriceEntry(
                provider="bailian",
                model=model,
                effective_from="2020-01-01",  # pyright: ignore[reportArgumentType]
                units={
                    UsageUnit.INPUT_TOKENS: per_million("0.8"),
                    UsageUnit.CACHED_TOKENS: per_million("0.32"),
                    UsageUnit.OUTPUT_TOKENS: per_million("2"),
                },
            )
        ]
    )


class FakeSession:
    """只记录 add_all / commit 的会话；sync_session 用真实 Session 以便 bind_user。"""

    def __init__(self, store: list[UsageEvent]) -> None:
        self.store = store
        self.sync_session = Session()
        self._pending: list[UsageEvent] = []

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(
        self, *_exc: type[BaseException] | BaseException | TracebackType | None
    ) -> None:
        assert bound_user(self.sync_session) == USER

    def add_all(self, objs: list[UsageEvent]) -> None:
        self._pending.extend(objs)

    async def commit(self) -> None:
        self.store.extend(self._pending)


def fake_sessionmaker(store: list[UsageEvent]) -> async_sessionmaker[AsyncSession]:
    return cast(Any, lambda: FakeSession(store))


@pytest.fixture
def exporter() -> Generator[InMemorySpanExporter]:
    exp = InMemorySpanExporter()
    init_tracing(
        "worker",
        ObservabilitySettings(enabled=True),
        environment="test",
        exporter=exp,
        set_global=False,
    )
    yield exp
    shutdown_tracing()


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_llm_quantities_splits_cached_and_reasoning() -> None:
    q = llm_quantities(
        prompt_tokens=1000, completion_tokens=300, cached_tokens=600, reasoning_tokens=100
    )
    assert q == {
        UsageUnit.INPUT_TOKENS: 400,
        UsageUnit.CACHED_TOKENS: 600,
        UsageUnit.OUTPUT_TOKENS: 200,
        UsageUnit.REASONING_TOKENS: 100,
    }
    # 厂商数据异常时不出现负数
    assert (
        llm_quantities(prompt_tokens=10, completion_tokens=5, cached_tokens=20)[
            UsageUnit.INPUT_TOKENS
        ]
        == 0
    )


async def test_records_rows_span_and_metrics(exporter: InMemorySpanExporter) -> None:
    store: list[UsageEvent] = []
    interview_id, turn_id = uuid.uuid4(), uuid.uuid4()
    labels = {"role": "front", "provider": "bailian", "model": "m-rows", "is_eval": "false"}
    with llm_span("front.reply", provider="bailian", model="m-rows") as current:
        result = await record_usage(
            user_id=USER,
            role=UsageRole.FRONT,
            provider="bailian",
            model="m-rows",
            quantities=llm_quantities(
                prompt_tokens=1_500_000, completion_tokens=500_000, cached_tokens=500_000
            ),
            interview_id=interview_id,
            turn_id=turn_id,
            is_retry=True,
            sessionmaker=fake_sessionmaker(store),
            prices=prices("m-rows"),
        )
        trace_id = format_trace_id(current.get_span_context().trace_id)

    # 0.8 + 0.32×0.5 + 2×0.5
    assert result.total_cost_cny == Decimal("1.96")
    assert result.persisted
    assert result.unpriced_units == ()
    # reasoning_tokens 为 0，不写入
    assert {e.unit: e.est_cost_cny for e in store} == {
        UsageUnit.INPUT_TOKENS: Decimal("0.8"),
        UsageUnit.CACHED_TOKENS: Decimal("0.16"),
        UsageUnit.OUTPUT_TOKENS: Decimal("1"),
    }
    for e in store:
        assert (e.user_id, e.interview_id, e.turn_id) == (USER, interview_id, turn_id)
        assert e.trace_id == trace_id
        assert (e.is_retry, e.is_eval, e.is_fallback) == (True, False, False)

    (span,) = exporter.get_finished_spans()
    attrs = span.attributes or {}
    assert json.loads(str(attrs[LF.USAGE_DETAILS])) == {
        "input": 1_000_000,
        "input_cached_tokens": 500_000,
        "output": 500_000,
    }
    costs = json.loads(str(attrs[LF.COST_DETAILS]))
    assert costs["total"] == pytest.approx(1.96)
    assert costs["input_cached_tokens"] == pytest.approx(0.16)
    assert attrs[LF.METADATA + "cost_currency"] == "CNY"

    assert sample("interview_cost_cny_total", **labels) == pytest.approx(1.96)
    assert sample("interview_tokens_total", **labels, unit="cached_tokens") == 500_000


async def test_audio_units_are_not_counted_as_tokens() -> None:
    store: list[UsageEvent] = []
    await record_usage(
        user_id=USER,
        role=UsageRole.ASR,
        provider="bailian",
        model="m-asr",
        quantities={UsageUnit.AUDIO_SECONDS: 12.5},
        sessionmaker=fake_sessionmaker(store),
        prices=prices("m-asr"),
    )
    assert store[0].quantity == Decimal("12.5")
    labels = {"role": "asr", "provider": "bailian", "model": "m-asr", "is_eval": "false"}
    assert sample("interview_tokens_total", **labels, unit="audio_seconds") == 0


async def test_missing_price_records_null_cost_without_raising(
    exporter: InMemorySpanExporter,
) -> None:
    store: list[UsageEvent] = []
    with llm_span("eval", provider="bailian", model="m-partial"):
        result = await record_usage(
            user_id=USER,
            role=UsageRole.EVAL,
            provider="bailian",
            model="m-partial",
            quantities={UsageUnit.INPUT_TOKENS: M, UsageUnit.REASONING_TOKENS: 100},
            sessionmaker=fake_sessionmaker(store),
            prices=prices("m-partial"),
        )
    assert result.total_cost_cny is None
    assert result.unpriced_units == (UsageUnit.REASONING_TOKENS,)
    assert {e.unit: e.est_cost_cny for e in store} == {
        UsageUnit.INPUT_TOKENS: Decimal("0.8"),
        UsageUnit.REASONING_TOKENS: None,
    }
    attrs = exporter.get_finished_spans()[0].attributes or {}
    # Langfuse 显示已知部分，并标出缺价格的单位
    assert json.loads(str(attrs[LF.COST_DETAILS])) == {"input": 0.8, "total": 0.8}
    assert attrs[LF.METADATA + "unpriced_units"] == "reasoning_tokens"
    labels = {"role": "eval", "provider": "bailian", "model": "m-partial", "is_eval": "false"}
    assert sample("interview_cost_cny_total", **labels) == pytest.approx(0.8)


async def test_is_eval_is_a_separate_metric_series() -> None:
    store: list[UsageEvent] = []
    for is_eval in (True, True, False):
        await record_usage(
            user_id=USER,
            role=UsageRole.EVAL_HARNESS,
            provider="bailian",
            model="m-eval",
            quantities={UsageUnit.INPUT_TOKENS: M},
            is_eval=is_eval,
            sessionmaker=fake_sessionmaker(store),
            prices=prices("m-eval"),
        )
    base = {"role": "eval_harness", "provider": "bailian", "model": "m-eval"}
    assert sample("interview_cost_cny_total", **base, is_eval="true") == pytest.approx(1.6)
    assert sample("interview_cost_cny_total", **base, is_eval="false") == pytest.approx(0.8)
    assert [e.is_eval for e in store] == [True, True, False]


async def test_database_failure_does_not_raise() -> None:
    # 不可达地址：连接失败
    engine = create_engine("postgresql+asyncpg://u:p@127.0.0.1:1/db")
    before = sample("interview_usage_record_failures_total", role="tts")
    with capture_logs() as logs:
        result = await record_usage(
            user_id=USER,
            role=UsageRole.TTS,
            provider="bailian",
            model="m-fail",
            quantities={UsageUnit.CHARACTERS: 120},
            sessionmaker=make_sessionmaker(engine),
            prices=prices("m-fail"),
        )
    await engine.dispose()
    assert not result.persisted
    assert len(result.events) == 1
    assert sample("interview_usage_record_failures_total", role="tts") == before + 1
    assert any(e["event"] == "usage_record_failed" for e in logs)


async def test_negative_quantity_is_a_caller_bug() -> None:
    with pytest.raises(ValueError, match="负数"):
        await record_usage(
            user_id=USER,
            role=UsageRole.FRONT,
            provider="bailian",
            model="m-neg",
            quantities={UsageUnit.INPUT_TOKENS: -1},
            sessionmaker=fake_sessionmaker([]),
            prices=prices("m-neg"),
        )


async def test_all_zero_quantities_write_nothing() -> None:
    store: list[UsageEvent] = []
    result = await record_usage(
        user_id=USER,
        role=UsageRole.FRONT,
        provider="bailian",
        model="m-zero",
        quantities={UsageUnit.INPUT_TOKENS: 0},
        sessionmaker=fake_sessionmaker(store),
        prices=prices("m-zero"),
    )
    assert store == []
    assert result.persisted
    assert result.total_cost_cny == 0
