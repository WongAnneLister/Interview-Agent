"""按配置里的角色（llm_front、llm_eval、stt、tts）创建厂商实例。

- 每个角色的 PROVIDER 名字通过工厂表找到实现：fake、bailian、deepseek
  （识别、合成：fake、bailian；合成另有 bailian_realtime，即 Qwen-TTS Realtime）。
- 底层适配器每个角色只建一次（首次取用时），以便复用连接；每次取实例都用调用方的
  CallContext 新包一层观测包装（很轻），interview_id、turn_id 在这时绑定。
- LLM 主 + 备：配置了 LLM_FALLBACK_* 时，主实例抛 Timeout / Unavailable / RateLimited 就切到备用，
  记 warning 日志，备用的用量标记 is_fallback。流式调用只在吐出第一个片段之前切换，
  之后再出错直接抛出（已经说出去的话不能重来）。适配器内部的重试在切换之前完成。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Literal, Self

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.config import (
    ConfigError,
    ProviderRoleSettings,
    ProvidersSettings,
    TTSSettings,
    VendorSettings,
)
from interview.observability.pricing import PriceTable
from interview.providers.bailian_asr import BailianASR
from interview.providers.bailian_tts import BailianTTS
from interview.providers.bailian_tts_realtime import BailianTTSRealtime
from interview.providers.base import (
    FAILOVER_ERRORS,
    ChatMessage,
    LLMProvider,
    LLMResult,
    LLMStream,
    LLMUsage,
    STTProvider,
    TTSProvider,
)
from interview.providers.fake import FakeLLM, FakeSTT, FakeTTS
from interview.providers.observed import (
    DEFAULT_USAGE_ROLES,
    CallContext,
    ObservedLLM,
    ObservedSTT,
    ObservedTTS,
    Observer,
    Slot,
)
from interview.providers.openai_compat import OpenAICompatLLM

__all__ = [
    "CallContext",
    "ProviderFactories",
    "ProviderNotConfigured",
    "ProviderRegistry",
    "default_factories",
]

LLMSlot = Literal["llm_front", "llm_eval"]

log = structlog.get_logger(__name__)


class ProviderNotConfigured(ConfigError):
    """角色未配置，或配置的厂商名没有对应实现。"""


@dataclass(frozen=True)
class ProviderFactories:
    """厂商名 → 工厂函数（接收该角色的配置）。"""

    llm: Mapping[str, Callable[[ProviderRoleSettings], LLMProvider]] = field(
        default_factory=lambda: {"fake": lambda s: FakeLLM(model=s.model)}
    )
    stt: Mapping[str, Callable[[ProviderRoleSettings], STTProvider]] = field(
        default_factory=lambda: {"fake": lambda s: FakeSTT(model=s.model)}
    )
    tts: Mapping[str, Callable[[ProviderRoleSettings], TTSProvider]] = field(
        default_factory=lambda: {"fake": lambda s: FakeTTS(model=s.model)}
    )


def openai_compat_factory(
    provider: str, vendor: VendorSettings
) -> Callable[[ProviderRoleSettings], LLMProvider]:
    """OpenAI 兼容厂商的工厂：密钥取厂商级配置，地址优先用角色配置的 BASE_URL。"""
    prefix = provider.upper()

    def create(role: ProviderRoleSettings) -> LLMProvider:
        base_url = role.base_url or vendor.base_url
        if not vendor.configured or not base_url:
            raise ProviderNotConfigured(f"{prefix}_API_KEY / {prefix}_BASE_URL 未配置")
        return OpenAICompatLLM(
            provider=provider,
            model=role.model,
            api_key=vendor.api_key.get_secret_value(),
            base_url=base_url,
            timeout_s=role.timeout_s,
            extra_body=role.extra_body,
        )

    return create


def bailian_asr_factory(vendor: VendorSettings) -> Callable[[ProviderRoleSettings], STTProvider]:
    """百炼流式识别：密钥取 BAILIAN_API_KEY，WebSocket 地址必须在 STT_BASE_URL 配置。"""

    def create(role: ProviderRoleSettings) -> STTProvider:
        if not vendor.configured or not role.base_url:
            raise ProviderNotConfigured("BAILIAN_API_KEY / STT_BASE_URL 未配置")
        return BailianASR(
            model=role.model,
            api_key=vendor.api_key.get_secret_value(),
            url=role.base_url,
            timeout_s=role.timeout_s,
            extra_params=role.extra_body,
        )

    return create


def bailian_tts_factory(
    vendor: VendorSettings, *, realtime: bool = False
) -> Callable[[ProviderRoleSettings], TTSProvider]:
    """百炼流式合成：需要 BAILIAN_API_KEY、TTS_BASE_URL、TTS_VOICE。

    realtime=False：CosyVoice / Qwen-Audio-TTS 任务协议；
    realtime=True：Qwen-TTS Realtime 协议（Qwen3-TTS 音色，如 Ryan、Bodega）。
    """
    cls = BailianTTSRealtime if realtime else BailianTTS

    def create(role: ProviderRoleSettings) -> TTSProvider:
        voice = role.voice if isinstance(role, TTSSettings) else ""
        if not vendor.configured or not role.base_url or not voice:
            raise ProviderNotConfigured("BAILIAN_API_KEY / TTS_BASE_URL / TTS_VOICE 未配置")
        return cls(
            model=role.model,
            api_key=vendor.api_key.get_secret_value(),
            url=role.base_url,
            timeout_s=role.timeout_s,
            voice=voice,
            extra_params=role.extra_body,
        )

    return create


def default_factories(settings: ProvidersSettings) -> ProviderFactories:
    """生产用的工厂表：假实现 + 已接入的真实厂商。"""
    fakes = ProviderFactories()
    return ProviderFactories(
        llm={
            **fakes.llm,
            "bailian": openai_compat_factory("bailian", settings.bailian),
            "deepseek": openai_compat_factory("deepseek", settings.deepseek),
        },
        stt={**fakes.stt, "bailian": bailian_asr_factory(settings.bailian)},
        tts={
            **fakes.tts,
            "bailian": bailian_tts_factory(settings.bailian),
            "bailian_realtime": bailian_tts_factory(settings.bailian, realtime=True),
        },
    )


class ProviderRegistry:
    def __init__(
        self,
        settings: ProvidersSettings,
        *,
        factories: ProviderFactories | None = None,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        prices: PriceTable | None = None,
    ) -> None:
        self._settings = settings
        self._factories = factories or default_factories(settings)
        self._sessionmaker = sessionmaker
        self._prices = prices
        self._llms: dict[Slot, LLMProvider] = {}
        self._stt: STTProvider | None = None
        self._tts: TTSProvider | None = None

    def _role_settings(self, slot: Slot) -> ProviderRoleSettings:
        role: ProviderRoleSettings = getattr(self._settings, slot)
        if not role.configured:
            prefix = slot.upper()
            raise ProviderNotConfigured(f"{prefix}_PROVIDER / {prefix}_MODEL 未配置")
        return role

    def _create[P](
        self, slot: Slot, factories: Mapping[str, Callable[[ProviderRoleSettings], P]]
    ) -> P:
        role = self._role_settings(slot)
        factory = factories.get(role.provider)
        if factory is None:
            choices = ", ".join(factories)
            raise ProviderNotConfigured(
                f"{slot.upper()}_PROVIDER={role.provider} 没有对应实现（可选：{choices}）"
            )
        return factory(role)

    def _observer(self, ctx: CallContext, slot: Slot, *, is_fallback: bool = False) -> Observer:
        return Observer(
            ctx=ctx,
            slot=slot,
            role=ctx.usage_role or DEFAULT_USAGE_ROLES[slot],
            is_fallback=is_fallback,
            sessionmaker=self._sessionmaker,
            prices=self._prices,
        )

    def _raw_llm(self, slot: Slot) -> LLMProvider:
        if slot not in self._llms:
            self._llms[slot] = self._create(slot, self._factories.llm)
        return self._llms[slot]

    def llm(self, slot: LLMSlot, ctx: CallContext) -> LLMProvider:
        primary = ObservedLLM(self._raw_llm(slot), self._observer(ctx, slot))
        if not self._settings.llm_fallback.configured:
            return primary
        # 备用实例的用量按调用方的角色和 slot 记账（如 front），用 is_fallback 区分
        observer = self._observer(ctx, slot, is_fallback=True)
        backup = ObservedLLM(self._raw_llm("llm_fallback"), observer)
        return FallbackLLM(primary, backup, slot=slot)

    def stt(self, ctx: CallContext) -> STTProvider:
        if self._stt is None:
            self._stt = self._create("stt", self._factories.stt)
        return ObservedSTT(self._stt, self._observer(ctx, "stt"))

    def tts(self, ctx: CallContext) -> TTSProvider:
        if self._tts is None:
            self._tts = self._create("tts", self._factories.tts)
        return ObservedTTS(self._tts, self._observer(ctx, "tts"))

    async def aclose(self) -> None:
        """关闭所有已创建的适配器（进程退出前调用）。"""
        for provider in [*self._llms.values(), self._stt, self._tts]:
            if provider is not None:
                await provider.aclose()
        self._llms.clear()
        self._stt = self._tts = None


def _log_failover(slot: Slot, primary: LLMProvider, backup: LLMProvider, exc: Exception) -> None:
    log.warning(
        "llm_fallback",
        slot=slot,
        provider=primary.provider,
        model=primary.model,
        fallback_provider=backup.provider,
        fallback_model=backup.model,
        error=type(exc).__name__,
    )


class FallbackLLM:
    """主备切换；provider / model 显示主实例的。"""

    def __init__(self, primary: LLMProvider, backup: LLMProvider, *, slot: Slot) -> None:
        self._primary = primary
        self._backup = backup
        self._slot: Slot = slot

    @property
    def provider(self) -> str:
        return self._primary.provider

    @property
    def model(self) -> str:
        return self._primary.model

    def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> LLMStream:
        def start(target: LLMProvider) -> LLMStream:
            return target.stream_chat(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout_s=timeout_s,
                extra=extra,
            )

        return _FailoverStream(
            start(self._primary),
            lambda: start(self._backup),
            lambda exc: _log_failover(self._slot, self._primary, self._backup, exc),
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
        kwargs: dict[str, Any] = {
            "max_tokens": max_tokens,
            "temperature": temperature,
            "timeout_s": timeout_s,
            "extra": extra,
        }
        try:
            return await self._primary.complete(messages, **kwargs)
        except FAILOVER_ERRORS as exc:
            _log_failover(self._slot, self._primary, self._backup, exc)
            return await self._backup.complete(messages, **kwargs)

    async def aclose(self) -> None:
        await self._primary.aclose()
        await self._backup.aclose()


class _FailoverStream:
    def __init__(
        self,
        primary: LLMStream,
        start_backup: Callable[[], LLMStream],
        on_failover: Callable[[Exception], None],
    ) -> None:
        self._current = primary
        self._start_backup = start_backup
        self._on_failover = on_failover
        self._started = False  # 已经吐出过片段，或已经切换过

    @property
    def usage(self) -> LLMUsage | None:
        return self._current.usage

    @property
    def first_latency_ms(self) -> float | None:
        return self._current.first_latency_ms

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> str:
        try:
            chunk = await anext(self._current)
        except FAILOVER_ERRORS as exc:
            if self._started:
                raise
            self._started = True
            self._on_failover(exc)
            self._current = self._start_backup()
            chunk = await anext(self._current)
        self._started = True
        return chunk

    async def aclose(self) -> None:
        await self._current.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
