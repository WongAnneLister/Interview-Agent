"""注册表与观测包装：按配置取实例、主备切换、span 与用量记录、取消合成。"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Generator
from decimal import Decimal
from types import TracebackType
from typing import Any, cast

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from interview.config import (
    LLMEvalSettings,
    LLMFallbackSettings,
    LLMFrontSettings,
    ObservabilitySettings,
    ProviderRoleSettings,
    ProvidersSettings,
    STTSettings,
    TTSSettings,
)
from interview.db.models import UsageEvent, UsageRole, UsageUnit
from interview.observability.pricing import PriceTable
from interview.observability.tracing import LF, Attr, init_tracing, shutdown_tracing, span
from interview.providers.base import (
    AuthError,
    BadResponse,
    ChatMessage,
    LLMProvider,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
)
from interview.providers.fake import FakeLLM, FakeSTT, FakeTTS
from interview.providers.observed import ObservedLLM
from interview.providers.registry import (
    CallContext,
    ProviderFactories,
    ProviderNotConfigured,
    ProviderRegistry,
)

USER = uuid.uuid4()
MESSAGES: list[ChatMessage] = [{"role": "user", "content": "介绍一下你自己"}]
GEN = {"max_tokens": 50, "temperature": 0.2}


class RecordingSession:
    def __init__(self, store: list[UsageEvent]) -> None:
        self.store = store
        self.sync_session = Session()  # bind_user 需要真实的同步会话

    async def __aenter__(self) -> RecordingSession:
        return self

    async def __aexit__(
        self, *_exc: type[BaseException] | BaseException | TracebackType | None
    ) -> None:
        return None

    def add_all(self, objs: list[UsageEvent]) -> None:
        self.store.extend(objs)

    async def commit(self) -> None:
        return None


@pytest.fixture
def store() -> list[UsageEvent]:
    return []


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


def role(cls: type[ProviderRoleSettings], provider: str, model: str) -> Any:
    return cls(provider=provider, model=model)  # pyright: ignore[reportCallIssue]


def make_registry(
    store: list[UsageEvent],
    *,
    llms: dict[str, LLMProvider] | None = None,
    fallback: str = "",
    tts: str = "fake",
    stt: FakeSTT | None = None,
    fake_tts: FakeTTS | None = None,
    llm_factory: Any = None,
) -> ProviderRegistry:
    llms = llms or {"fake": FakeLLM("你好，我是面试官")}
    front = next(iter(llms))
    settings = ProvidersSettings(
        llm_front=role(LLMFrontSettings, front, "front-m"),
        llm_eval=role(LLMEvalSettings, front, "eval-m"),
        llm_fallback=role(LLMFallbackSettings, fallback, "backup-m" if fallback else ""),
        stt=role(STTSettings, "fake", "stt-m"),
        tts=role(TTSSettings, tts, "tts-m"),
    )
    factories = ProviderFactories(
        llm=llm_factory or {name: (lambda _s, p=p: p) for name, p in llms.items()},
        stt={"fake": lambda _s: stt or FakeSTT()},
        tts={"fake": lambda _s: fake_tts or FakeTTS()},
    )

    def sessionmaker() -> RecordingSession:
        return RecordingSession(store)

    return ProviderRegistry(
        settings,
        factories=factories,
        sessionmaker=cast(async_sessionmaker[AsyncSession], sessionmaker),
        prices=PriceTable(),
    )


async def texts(*chunks: str) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


async def frames(count: int) -> AsyncIterator[bytes]:
    for _ in range(count):
        yield b"\0" * 320


def attrs_of(s: ReadableSpan) -> dict[str, Any]:
    return dict(s.attributes or {})


def ctx(**kwargs: Any) -> CallContext:
    return CallContext(user_id=USER, **kwargs)


# ---- 注册表 ----


def test_default_factories_build_fakes_from_settings() -> None:
    settings = ProvidersSettings(
        llm_front=role(LLMFrontSettings, "fake", "front-m"),
        llm_eval=role(LLMEvalSettings, "fake", "eval-m"),
        llm_fallback=role(LLMFallbackSettings, "", ""),
        stt=role(STTSettings, "fake", "stt-m"),
        tts=role(TTSSettings, "fake", "tts-m"),
    )
    registry = ProviderRegistry(settings)
    assert isinstance(registry.llm("llm_front", ctx()), ObservedLLM)  # 未配置备用时不包主备
    assert registry.llm("llm_eval", ctx()).model == "eval-m"
    assert (registry.stt(ctx()).model, registry.tts(ctx()).model) == ("stt-m", "tts-m")


async def test_adapters_are_created_once_and_closed(store: list[UsageEvent]) -> None:
    created: list[str] = []
    fake = FakeLLM()

    def factory(s: ProviderRoleSettings) -> LLMProvider:
        created.append(s.model)
        return fake

    registry = make_registry(store, llms={"fake": fake}, llm_factory={"fake": factory})
    first = registry.llm("llm_front", ctx())
    registry.llm("llm_front", ctx(turn_id=uuid.uuid4()))
    registry.llm("llm_eval", ctx())
    assert created == ["front-m", "eval-m"]
    await first.aclose()  # 包装实例不关闭共享的适配器
    assert not fake.closed
    await registry.aclose()
    assert fake.closed


def test_unconfigured_or_unknown_provider_raises(store: list[UsageEvent]) -> None:
    with pytest.raises(ProviderNotConfigured, match="TTS_PROVIDER"):
        make_registry(store, tts="").tts(ctx())
    with pytest.raises(ProviderNotConfigured, match="可选：fake"):
        make_registry(store, tts="nope").tts(ctx())


# ---- 主备切换 ----


def failover_registry(
    store: list[UsageEvent], primary: FakeLLM, backup: FakeLLM
) -> ProviderRegistry:
    return make_registry(store, llms={"primary": primary, "backup": backup}, fallback="backup")


async def test_complete_fails_over_and_marks_usage(
    store: list[UsageEvent], exporter: InMemorySpanExporter
) -> None:
    primary = FakeLLM("主", model="front-m", errors=[ProviderTimeout("超时")])
    backup = FakeLLM("备用回答", model="backup-m")
    llm = failover_registry(store, primary, backup).llm("llm_front", ctx())
    with capture_logs() as logs:
        result = await llm.complete(MESSAGES, **GEN)
    assert result.text == "备用回答"
    (event,) = [e for e in logs if e["event"] == "llm_fallback"]
    assert event["error"] == "ProviderTimeout"
    assert (event["provider"], event["fallback_model"]) == ("fake", "backup-m")
    assert {e.model for e in store} == {"backup-m"}
    assert all(e.is_fallback and e.role == UsageRole.FRONT for e in store)

    failed, ok = exporter.get_finished_spans()
    assert failed.status.status_code == StatusCode.ERROR
    assert attrs_of(ok)["is_fallback"] is True


@pytest.mark.parametrize("error", [RateLimited("限流"), ProviderUnavailable("断开")])
async def test_stream_fails_over_before_first_chunk(
    store: list[UsageEvent], error: Exception
) -> None:
    primary = FakeLLM("主", errors=[cast(Any, error)])
    backup = FakeLLM("备用", chunk_size=1)
    llm = failover_registry(store, primary, backup).llm("llm_eval", ctx())
    async with llm.stream_chat(MESSAGES, **GEN) as stream:
        assert [c async for c in stream] == ["备", "用"]
        assert stream.usage is not None
        assert stream.usage.completion_tokens == 2
    assert len(backup.calls) == 1


async def test_stream_does_not_fail_over_after_first_chunk(store: list[UsageEvent]) -> None:
    primary = FakeLLM("abcdef", chunk_size=2, fail_after_chunks=1)
    backup = FakeLLM("备用")
    stream = (
        failover_registry(store, primary, backup)
        .llm("llm_front", ctx())
        .stream_chat(MESSAGES, **GEN)
    )
    assert await anext(stream) == "ab"
    with pytest.raises(ProviderUnavailable):
        await anext(stream)
    assert backup.calls == []
    # 已产生的部分照常记账
    assert {e.unit: e.quantity for e in store}[UsageUnit.OUTPUT_TOKENS] == 2


@pytest.mark.parametrize("error", [AuthError("密钥错误"), BadResponse("格式错误")])
async def test_non_failover_errors_propagate(store: list[UsageEvent], error: Exception) -> None:
    primary = FakeLLM("主", errors=[cast(Any, error)])
    backup = FakeLLM("备用")
    llm = failover_registry(store, primary, backup).llm("llm_front", ctx())
    with pytest.raises(type(error)):
        await llm.complete(MESSAGES, **GEN)
    assert backup.calls == []
    assert store == []


# ---- 观测包装 ----


async def test_llm_stream_span_and_usage(
    store: list[UsageEvent], exporter: InMemorySpanExporter
) -> None:
    interview_id, turn_id = uuid.uuid4(), uuid.uuid4()
    llm = make_registry(store).llm("llm_front", ctx(interview_id=interview_id, turn_id=turn_id))
    with span("turn") as parent:
        stream = llm.stream_chat(MESSAGES, **GEN, extra={"enable_thinking": False})
        assert await anext(stream) == "你好，我"
        # 两次取值之间，流的 span 不是当前 span
        assert trace.get_current_span() is parent
        rest = [c async for c in stream]
    assert "".join(["你好，我", *rest]) == "你好，我是面试官"

    spans = {s.name: s for s in exporter.get_finished_spans()}
    s = spans["llm.stream_chat"]
    assert s.parent is not None
    assert s.parent.span_id == spans["turn"].context.span_id  # pyright: ignore[reportOptionalMemberAccess]
    a = attrs_of(s)
    assert a[Attr.INTERVIEW_ID] == str(interview_id)
    assert a[Attr.TURN_ID] == str(turn_id)
    assert (a[Attr.AGENT_ROLE], a[Attr.PROVIDER], a[Attr.MODEL]) == ("front", "fake", "fake-llm")
    assert (a["provider.slot"], a["is_fallback"]) == ("llm_front", False)
    assert a[LF.TYPE] == "generation"
    assert a[LF.OUTPUT] == "你好，我是面试官"
    assert json.loads(a[LF.MODEL_PARAMETERS])["enable_thinking"] is False
    assert LF.COMPLETION_START_TIME in a
    assert a["first_latency_ms"] >= 0
    assert json.loads(a[LF.USAGE_DETAILS]) == {"input": 7, "output": 8}

    assert {e.unit: e.quantity for e in store} == {
        UsageUnit.INPUT_TOKENS: 7,
        UsageUnit.OUTPUT_TOKENS: 8,
    }
    for e in store:
        assert (e.user_id, e.interview_id, e.turn_id) == (USER, interview_id, turn_id)
        assert (e.role, e.provider, e.model, e.is_fallback) == (
            UsageRole.FRONT,
            "fake",
            "fake-llm",
            False,
        )


async def test_complete_uses_context_usage_role(
    store: list[UsageEvent], exporter: InMemorySpanExporter
) -> None:
    llm = make_registry(store).llm("llm_eval", ctx(usage_role=UsageRole.REPORT, is_eval=True))
    await llm.complete(MESSAGES, **GEN)
    (s,) = exporter.get_finished_spans()
    assert s.name == "llm.complete"
    a = attrs_of(s)
    assert (a[Attr.AGENT_ROLE], a["provider.slot"], a[Attr.IS_EVAL]) == ("report", "llm_eval", True)
    assert a[LF.OUTPUT] == "你好，我是面试官"
    assert {(e.role, e.is_eval) for e in store} == {(UsageRole.REPORT, True)}


async def test_error_span_without_usage(
    store: list[UsageEvent], exporter: InMemorySpanExporter
) -> None:
    llm = make_registry(store, llms={"fake": FakeLLM(errors=[BadResponse("坏数据")])})
    stream = llm.llm("llm_front", ctx()).stream_chat(MESSAGES, **GEN)
    with pytest.raises(BadResponse):
        await anext(stream)
    (s,) = exporter.get_finished_spans()
    assert s.status.status_code == StatusCode.ERROR
    assert store == []


async def test_stt_span_output_and_audio_seconds(
    store: list[UsageEvent], exporter: InMemorySpanExporter
) -> None:
    stt = make_registry(store).stt(ctx())
    events = [e async for e in stt.stream(frames(4), sample_rate=16000)]
    assert [e.kind for e in events] == ["partial", "final"]
    (s,) = exporter.get_finished_spans()
    a = attrs_of(s)
    assert (s.name, a[Attr.AGENT_ROLE], a[LF.OUTPUT]) == ("stt.stream", "asr", "你好。")
    assert {e.unit: e.quantity for e in store} == {UsageUnit.AUDIO_SECONDS: Decimal("0.04")}


async def test_cancelled_tts_records_partial_usage(
    store: list[UsageEvent], exporter: InMemorySpanExporter
) -> None:
    fake = FakeTTS(frame_ms=10)
    tts = make_registry(store, fake_tts=fake).tts(ctx())
    async with tts.synthesize(texts("很长的一句话"), voice="v1", sample_rate=16000) as stream:
        await anext(stream)
    assert fake.cancelled == 1

    (s,) = exporter.get_finished_spans()
    a = attrs_of(s)
    assert (s.name, a["cancelled"], a["voice"]) == ("tts.synthesize", True, "v1")
    assert s.status.status_code != StatusCode.ERROR
    assert a[LF.INPUT] == "很长的一句话"
    assert {(e.unit, e.quantity, e.role) for e in store} == {
        (UsageUnit.CHARACTERS, 6, UsageRole.TTS)
    }


async def test_closing_unstarted_stream_records_nothing(
    store: list[UsageEvent], exporter: InMemorySpanExporter
) -> None:
    stream = make_registry(store).tts(ctx()).synthesize(texts("不会合成"))
    await stream.aclose()
    assert exporter.get_finished_spans() == ()
    assert store == []


async def test_warmup_is_forwarded_with_span(
    store: list[UsageEvent], exporter: InMemorySpanExporter
) -> None:
    fake_tts = FakeTTS()
    registry = make_registry(store, fake_tts=fake_tts)
    await registry.tts(ctx()).warmup()
    assert fake_tts.warmups == 1
    (s,) = exporter.get_finished_spans()
    assert (s.name, attrs_of(s)[Attr.AGENT_ROLE]) == ("tts.warmup", "tts")
    assert store == []
