"""观测包装：注册表返回的每个实例外面包一层，适配器本身只负责返回用量数字。

每次调用：
- 开一个 span（角色、slot、厂商、型号、interview_id、turn_id、is_eval、is_fallback、首包耗时），
  大模型调用在 Langfuse 里是 generation（输入消息、参数、输出文本、首 token 时间）。
  流式调用的 span 从第一次取值开始，到迭代结束、aclose()、出错或任务被取消时结束；
  被取消时标记 cancelled=true，不算错误。
- 结束时把适配器返回的用量换算成计量单位，调用 usage.record_usage()（取消时记录已产生的部分）。
- 在 marked_retry() 块内发起的调用（如结构化输出校验失败后的重试）用量标记 is_retry，
  span 也带 is_retry。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Literal, Self

from opentelemetry.trace import Span
from opentelemetry.util.types import AttributeValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.db.models import UsageRole, UsageUnit
from interview.observability import usage as usage_module
from interview.observability.pricing import PriceTable
from interview.observability.tracing import (
    LF,
    OpenSpan,
    llm_attributes,
    llm_span,
    open_span,
    set_llm_output,
    span,
)
from interview.providers.base import (
    ChatMessage,
    LLMProvider,
    LLMResult,
    LLMStream,
    LLMUsage,
    ProviderStream,
    STTEvent,
    STTProvider,
    STTStream,
    STTUsage,
    TTSProvider,
    TTSStream,
    TTSUsage,
)

Slot = Literal["llm_front", "llm_eval", "llm_fallback", "stt", "tts"]

# 调用方没有指定 usage_role 时，按 slot 推断
DEFAULT_USAGE_ROLES: Mapping[Slot, UsageRole] = {
    "llm_front": UsageRole.FRONT,
    "llm_eval": UsageRole.EVAL,
    "stt": UsageRole.ASR,
    "tts": UsageRole.TTS,
}


_is_retry: ContextVar[bool] = ContextVar("provider_call_is_retry", default=False)


@contextmanager
def marked_retry(enabled: bool = True) -> Generator[None]:
    """块内发起的厂商调用记为重试；嵌套时保留外层的重试标记。"""
    token = _is_retry.set(enabled or _is_retry.get())
    try:
        yield
    finally:
        _is_retry.reset(token)


@dataclass(frozen=True)
class CallContext:
    """一组调用共用的上下文，在向注册表取实例时绑定。"""

    user_id: uuid.UUID
    interview_id: uuid.UUID | None = None
    turn_id: uuid.UUID | None = None
    usage_role: UsageRole | None = None  # 同一个 llm_eval 会用于出题、评估、报告等，按用途指定
    is_eval: bool = False


@dataclass(frozen=True)
class Observer:
    """一个包装实例的观测参数：span 属性和用量记录。"""

    ctx: CallContext
    slot: Slot
    role: UsageRole
    is_fallback: bool = False
    sessionmaker: async_sessionmaker[AsyncSession] | None = None
    prices: PriceTable | None = None

    def named(self, provider: str, model: str) -> dict[str, Any]:
        """span() / llm_span() 的具名参数。"""
        ctx = self.ctx
        return {
            "interview_id": str(ctx.interview_id) if ctx.interview_id else None,
            "turn_id": str(ctx.turn_id) if ctx.turn_id else None,
            "role": self.role.value,
            "provider": provider,
            "model": model,
            "is_eval": ctx.is_eval,
        }

    def attrs(self) -> dict[str, AttributeValue]:
        return {
            "provider.slot": self.slot,
            "is_fallback": self.is_fallback,
            "is_retry": _is_retry.get(),
            LF.METADATA + "slot": self.slot,
            LF.METADATA + "is_fallback": self.is_fallback,
        }

    async def record(
        self, target: Span, provider: str, model: str, usage: LLMUsage | STTUsage | TTSUsage
    ) -> None:
        await usage_module.record_usage(
            user_id=self.ctx.user_id,
            role=self.role,
            provider=provider,
            model=model,
            quantities=quantities(usage),
            interview_id=self.ctx.interview_id,
            turn_id=self.ctx.turn_id,
            is_eval=self.ctx.is_eval,
            is_retry=_is_retry.get(),
            is_fallback=self.is_fallback,
            span=target,
            sessionmaker=self.sessionmaker,
            prices=self.prices,
        )


def quantities(usage: LLMUsage | STTUsage | TTSUsage) -> dict[UsageUnit, int | float]:
    match usage:
        case LLMUsage():
            return dict(
                usage_module.llm_quantities(
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    cached_tokens=usage.cached_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                )
            )
        case STTUsage():
            return {UsageUnit.AUDIO_SECONDS: usage.audio_seconds}
        case TTSUsage():
            return {UsageUnit.CHARACTERS: usage.characters}


# 结束时写入 span 的内容：(span, 收集到的文本, 首个结果的时间)
type Annotate = Callable[[Span, str, datetime | None], None]


class ObservedStream[T, U: LLMUsage | STTUsage | TTSUsage]:
    """给流对象加上 span 和用量记录；usage / first_latency_ms 直接取自内层流。"""

    def __init__(
        self,
        inner: ProviderStream[T, U],
        *,
        opener: Callable[[], OpenSpan],
        observer: Observer,
        provider: str,
        model: str,
        text_of: Callable[[T], str | None] = lambda _item: None,
        annotate: Annotate = lambda _span, _text, _first_at: None,
    ) -> None:
        self._inner = inner
        self._opener = opener
        self._observer = observer
        self._provider = provider
        self._model = model
        self._text_of = text_of
        self._annotate = annotate
        self._span: OpenSpan | None = None
        self._parts: list[str] = []
        self._first_at: datetime | None = None
        self._done = False

    @property
    def usage(self) -> U | None:
        return self._inner.usage

    @property
    def first_latency_ms(self) -> float | None:
        return self._inner.first_latency_ms

    def __aiter__(self) -> AsyncIterator[T]:
        return self

    async def __anext__(self) -> T:
        if self._done:
            raise StopAsyncIteration
        if self._span is None:
            self._span = self._opener()
        try:
            with self._span.activate():
                item = await anext(self._inner)
        except StopAsyncIteration:
            await self._finish()
            raise
        except asyncio.CancelledError:
            await self._finish(cancelled=True)
            raise
        except BaseException as exc:
            await self._finish(error=exc)
            raise
        if self._first_at is None:
            self._first_at = datetime.now(UTC)
        text = self._text_of(item)
        if text:
            self._parts.append(text)
        return item

    async def aclose(self) -> None:
        if self._done:
            return
        try:
            await self._inner.aclose()
        finally:
            if self._span is None:
                self._done = True  # 从未开始，不产生 span 和用量
            else:
                await self._finish(cancelled=True)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _finish(self, *, error: BaseException | None = None, cancelled: bool = False) -> None:
        self._done = True
        current = self._span
        if current is None:
            return
        try:
            target = current.span
            if cancelled:
                target.set_attribute("cancelled", True)
            if self._inner.first_latency_ms is not None:
                target.set_attribute("first_latency_ms", self._inner.first_latency_ms)
            self._annotate(target, "".join(self._parts), self._first_at)
            usage = self._inner.usage
            if usage is not None:
                with current.activate():
                    await self._observer.record(target, self._provider, self._model, usage)
        finally:
            current.end(error)


class ObservedLLM:
    def __init__(self, inner: LLMProvider, observer: Observer) -> None:
        self._inner = inner
        self._observer = observer

    @property
    def provider(self) -> str:
        return self._inner.provider

    @property
    def model(self) -> str:
        return self._inner.model

    def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> LLMStream:
        inner = self._inner.stream_chat(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_s=timeout_s,
            extra=extra,
        )
        params = {"max_tokens": max_tokens, "temperature": temperature, **(extra or {})}
        observer = self._observer
        kwargs: dict[str, Any] = {
            **observer.named(self.provider, self.model),
            **observer.attrs(),
            **llm_attributes(self.model, input=list(messages), model_parameters=params),
        }

        def annotate(target: Span, text: str, first_at: datetime | None) -> None:
            set_llm_output(target, text, completion_start_time=first_at)

        return ObservedStream(
            inner,
            opener=lambda: open_span("llm.stream_chat", observation_type="generation", **kwargs),
            observer=self._observer,
            provider=self.provider,
            model=self.model,
            text_of=lambda chunk: chunk,
            annotate=annotate,
        )

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> LLMResult:
        params = {"max_tokens": max_tokens, "temperature": temperature, **(extra or {})}
        with llm_span(
            "llm.complete",
            **self._observer.named(self.provider, self.model),
            input=list(messages),
            model_parameters=params,
        ) as target:
            target.set_attributes(self._observer.attrs())
            started_at = datetime.now(UTC)
            result = await self._inner.complete(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout_s=timeout_s,
                extra=extra,
            )
            first_at = None
            if result.first_token_ms is not None:
                target.set_attribute("first_latency_ms", result.first_token_ms)
                first_at = started_at + timedelta(milliseconds=result.first_token_ms)
            set_llm_output(target, result.text, completion_start_time=first_at)
            if result.usage is not None:
                await self._observer.record(target, self.provider, self.model, result.usage)
        return result

    async def aclose(self) -> None:
        """底层适配器由注册表共享并统一关闭，这里不关闭。"""


class ObservedSTT:
    def __init__(self, inner: STTProvider, observer: Observer) -> None:
        self._inner = inner
        self._observer = observer

    @property
    def provider(self) -> str:
        return self._inner.provider

    @property
    def model(self) -> str:
        return self._inner.model

    def stream(
        self,
        audio: AsyncIterable[bytes],
        *,
        sample_rate: int = 16000,
        language_hint: str | None = None,
        hotword_list_id: str | None = None,
    ) -> STTStream:
        inner = self._inner.stream(
            audio,
            sample_rate=sample_rate,
            language_hint=language_hint,
            hotword_list_id=hotword_list_id,
        )
        kwargs: dict[str, Any] = {
            **self._observer.named(self.provider, self.model),
            **self._observer.attrs(),
        }

        def text_of(event: STTEvent) -> str | None:
            return event.text if event.kind == "final" else None

        def annotate(target: Span, text: str, _first_at: datetime | None) -> None:
            target.set_attribute(LF.OUTPUT, text)

        return ObservedStream(
            inner,
            opener=lambda: open_span("stt.stream", **kwargs),
            observer=self._observer,
            provider=self.provider,
            model=self.model,
            text_of=text_of,
            annotate=annotate,
        )

    async def warmup(self) -> None:
        kwargs: dict[str, Any] = {
            **self._observer.named(self.provider, self.model),
            **self._observer.attrs(),
        }
        with span(f"{self._observer.slot}.warmup", **kwargs):
            await self._inner.warmup()

    async def aclose(self) -> None:
        """底层适配器由注册表共享并统一关闭，这里不关闭。"""


async def _tap(source: AsyncIterable[str], sink: list[str]) -> AsyncIterator[str]:
    async for chunk in source:
        sink.append(chunk)
        yield chunk


class ObservedTTS:
    def __init__(self, inner: TTSProvider, observer: Observer) -> None:
        self._inner = inner
        self._observer = observer

    @property
    def provider(self) -> str:
        return self._inner.provider

    @property
    def model(self) -> str:
        return self._inner.model

    def synthesize(
        self,
        text: AsyncIterable[str],
        *,
        voice: str | None = None,
        language: str | None = None,
        sample_rate: int = 24000,
    ) -> TTSStream:
        sent: list[str] = []
        inner = self._inner.synthesize(
            _tap(text, sent), voice=voice, language=language, sample_rate=sample_rate
        )
        kwargs: dict[str, Any] = {
            **self._observer.named(self.provider, self.model),
            **self._observer.attrs(),
        }
        if voice:
            kwargs["voice"] = voice

        def annotate(target: Span, _text: str, _first_at: datetime | None) -> None:
            target.set_attribute(LF.INPUT, "".join(sent))

        return ObservedStream(
            inner,
            opener=lambda: open_span("tts.synthesize", **kwargs),
            observer=self._observer,
            provider=self.provider,
            model=self.model,
            annotate=annotate,
        )

    async def warmup(self) -> None:
        kwargs: dict[str, Any] = {
            **self._observer.named(self.provider, self.model),
            **self._observer.attrs(),
        }
        with span(f"{self._observer.slot}.warmup", **kwargs):
            await self._inner.warmup()

    async def aclose(self) -> None:
        """底层适配器由注册表共享并统一关闭，这里不关闭。"""
