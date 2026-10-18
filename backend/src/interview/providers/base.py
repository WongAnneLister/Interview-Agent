"""厂商适配接口：大模型（LLM）、流式识别（STT）、流式合成（TTS）。

业务代码只依赖这里的 Protocol 和数据类型，不直接 import 厂商 SDK。

- 流式调用返回 ProviderStream：可直接 async for；建连发生在第一次取值时，连接错误也在那时抛出。
  迭代结束、aclose()（中途取消，如 TTS 被打断）或出错之后，可读取 usage 和 first_latency_ms。
- 适配器只负责返回用量数字；span 与用量入库由注册表返回的观测包装完成。
- 厂商错误由适配器映射到 ProviderError 的子类（含重试），包装层和主备切换只认这几类。
- 音频统一为 PCM16 小端单声道的 bytes，采样率由调用参数决定。
"""

from __future__ import annotations

import time
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    AsyncIterator,
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Protocol, Self, TypedDict

# ---- 错误层级 ----


class ProviderError(Exception):
    """厂商调用失败的基类。消息里不要放请求正文。"""

    def __init__(self, message: str, *, provider: str = "", model: str = "") -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model


class ProviderTimeout(ProviderError, TimeoutError):
    """连接、首包或整体超时。"""


class RateLimited(ProviderError):
    """被厂商限流（如 HTTP 429），适配器内的重试已用完。"""


class ProviderUnavailable(ProviderError):
    """厂商服务不可用（5xx、连接被拒、异常断开）。"""


class BadResponse(ProviderError):
    """厂商返回了无法解析或不符合协议的数据。"""


class AuthError(ProviderError):
    """鉴权失败或无权限（密钥错误、欠费等），切换备用也不该掩盖它，需要人工处理。"""


# 主实例抛出这几类错误时切换到备用
FAILOVER_ERRORS: tuple[type[ProviderError], ...] = (
    ProviderTimeout,
    RateLimited,
    ProviderUnavailable,
)


# ---- 数据类型 ----


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class LLMUsage:
    """厂商口径的 token 数：prompt 含缓存命中部分，completion 含推理部分。"""

    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class STTUsage:
    audio_seconds: float  # 计费时长，口径（连接时长或有效语音时长）由适配器决定


@dataclass(frozen=True)
class TTSUsage:
    characters: int  # 计费字符数


@dataclass(frozen=True)
class LLMResult:
    text: str
    usage: LLMUsage | None
    first_token_ms: float | None
    # 实际作答的厂商和型号；主备切换后是备用的，LLMProvider.model 始终是主实例的
    provider: str
    model: str


@dataclass(frozen=True)
class STTEvent:
    kind: Literal["partial", "final"]
    text: str
    start_ms: int
    end_ms: int
    language: str | None = None


# ---- 流对象 ----


class ProviderStream[T, U](Protocol):
    """流式调用的返回值：异步迭代 T，结束后可取得用量 U 和首包耗时。"""

    def __aiter__(self) -> AsyncIterator[T]: ...

    async def __anext__(self) -> T: ...

    async def aclose(self) -> None:
        """中途取消：停止接收并通知厂商结束任务；可重复调用。"""
        ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    @property
    def usage(self) -> U | None:
        """迭代结束或取消后可读；取消时为已经产生的部分，厂商未返回时为空。"""
        ...

    @property
    def first_latency_ms(self) -> float | None:
        """LLM：发出请求到首个 token；TTS：发出首段文本到首块音频；STT：首帧音频到首个事件。"""
        ...


type LLMStream = ProviderStream[str, LLMUsage]
type STTStream = ProviderStream[STTEvent, STTUsage]
type TTSStream = ProviderStream[bytes, TTSUsage]


class GeneratorStream[T, U]:
    """把 async generator 包装成 ProviderStream，适配器和假实现共用。

    body 接收流对象本身：在发出请求时调用 mark_start()，收到首个结果时调用 mark_first()，
    随时更新 usage（取消时也能读到已产生的用量）。body 在第一次取值时才开始执行。
    """

    def __init__(self, body: Callable[[GeneratorStream[T, U]], AsyncGenerator[T]]) -> None:
        self.usage: U | None = None
        self.first_latency_ms: float | None = None
        self._started_at: float | None = None
        self._gen = body(self)

    def mark_start(self) -> None:
        if self._started_at is None:
            self._started_at = time.perf_counter()

    def mark_first(self) -> None:
        if self.first_latency_ms is None and self._started_at is not None:
            self.first_latency_ms = (time.perf_counter() - self._started_at) * 1000

    def __aiter__(self) -> AsyncIterator[T]:
        return self

    async def __anext__(self) -> T:
        return await anext(self._gen)

    async def aclose(self) -> None:
        await self._gen.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


# ---- 三类接口 ----


class LLMProvider(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> LLMStream:
        """流式对话，迭代文本片段。timeout_s 为空时用该角色配置的超时；extra 透传厂商参数。"""
        ...

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> LLMResult:
        """一次性取得完整回答。"""
        ...

    async def aclose(self) -> None:
        """释放连接；进程退出前由注册表调用。"""
        ...


class STTProvider(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def stream(
        self,
        audio: AsyncIterable[bytes],
        *,
        sample_rate: int = 16000,
        language_hint: str | None = None,
        hotword_list_id: str | None = None,
    ) -> STTStream:
        """边送音频边出识别事件；audio 结束即表示说话结束，剩余 final 事件随后输出。"""
        ...

    async def warmup(self) -> None:
        """提前建好连接（如面试开始时、候选人开口前），避免把建连时间算进首包延迟。"""
        ...

    async def aclose(self) -> None: ...


class TTSProvider(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def synthesize(
        self,
        text: AsyncIterable[str],
        *,
        voice: str | None = None,
        language: str | None = None,
        sample_rate: int = 24000,
    ) -> TTSStream:
        """边送文本边出音频帧；打断时调用返回流的 aclose()。voice 为空时用适配器默认音色。"""
        ...

    async def warmup(self) -> None:
        """提前建好连接（如面试开始时、等大模型回答时），避免把建连时间算进首包延迟。"""
        ...

    async def aclose(self) -> None: ...
