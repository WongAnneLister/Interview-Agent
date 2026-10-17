"""可配置输出和延迟的假实现，用于所有单元测试和无密钥的本地开发（PROVIDER=fake）。

输出（文本、事件、音频帧）和用量都是确定的；延迟用 asyncio.sleep 模拟，耗时按真实时间测量。
errors 是按调用顺序消费的队列：第 n 次调用在首次取值时抛出第 n 项，None 表示这次正常。
"""

from __future__ import annotations

import asyncio
import struct
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterable, Callable, Iterable, Mapping, Sequence
from typing import Any

from interview.providers.base import (
    ChatMessage,
    GeneratorStream,
    LLMResult,
    LLMStream,
    LLMUsage,
    ProviderError,
    ProviderUnavailable,
    STTEvent,
    STTStream,
    STTUsage,
    TTSStream,
    TTSUsage,
)


def echo_reply(messages: Sequence[ChatMessage]) -> str:
    """默认回答：复述最后一条用户消息。"""
    last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    return f"[fake] {last}"


class _FakeBase:
    def __init__(self, provider: str, model: str, errors: Iterable[ProviderError | None]) -> None:
        self.provider = provider
        self.model = model
        self.calls: list[dict[str, Any]] = []  # 每次调用的参数，供测试断言
        self.closed = False
        self.warmups = 0
        self._errors: deque[ProviderError | None] = deque(errors)

    def _raise_scripted(self) -> None:
        error = self._errors.popleft() if self._errors else None
        if error is not None:
            raise error

    async def warmup(self) -> None:
        self.warmups += 1

    async def aclose(self) -> None:
        self.closed = True


class FakeLLM(_FakeBase):
    """按 chunk_size 个字切分回答；用量：prompt = 消息总字数，completion = 已输出字数。"""

    def __init__(
        self,
        reply: str | Callable[[Sequence[ChatMessage]], str] = echo_reply,
        *,
        provider: str = "fake",
        model: str = "fake-llm",
        chunk_size: int = 4,
        first_token_delay_s: float = 0.0,
        chunk_delay_s: float = 0.0,
        errors: Iterable[ProviderError | None] = (),
        fail_after_chunks: int | None = None,
    ) -> None:
        super().__init__(provider, model, errors)
        self._reply = reply
        self._chunk_size = chunk_size
        self._first_token_delay_s = first_token_delay_s
        self._chunk_delay_s = chunk_delay_s
        self._fail_after_chunks = fail_after_chunks  # 输出这么多片段后模拟连接断开

    def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> LLMStream:
        self.calls.append(
            {
                "messages": list(messages),
                "max_tokens": max_tokens,
                "temperature": temperature,
                "timeout_s": timeout_s,
                "extra": dict(extra or {}),
            }
        )
        reply = self._reply if isinstance(self._reply, str) else self._reply(messages)
        prompt_tokens = sum(len(m["content"]) for m in messages)

        async def body(stream: GeneratorStream[str, LLMUsage]) -> AsyncGenerator[str]:
            stream.mark_start()
            self._raise_scripted()
            stream.usage = LLMUsage(prompt_tokens=prompt_tokens, completion_tokens=0)
            await asyncio.sleep(self._first_token_delay_s)
            sent = 0
            for index in range(0, len(reply), self._chunk_size):
                if index and self._chunk_delay_s:
                    await asyncio.sleep(self._chunk_delay_s)
                if self._fail_after_chunks is not None and index // self._chunk_size >= (
                    self._fail_after_chunks
                ):
                    raise ProviderUnavailable("fake 连接断开", provider=self.provider)
                chunk = reply[index : index + self._chunk_size]
                sent += len(chunk)
                stream.usage = LLMUsage(prompt_tokens=prompt_tokens, completion_tokens=sent)
                stream.mark_first()
                yield chunk

        return GeneratorStream(body)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> LLMResult:
        async with self.stream_chat(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_s=timeout_s,
            extra=extra,
        ) as stream:
            text = "".join([chunk async for chunk in stream])
        return LLMResult(text=text, usage=stream.usage, first_token_ms=stream.first_latency_ms)


DEFAULT_STT_EVENTS = (
    STTEvent(kind="partial", text="你好", start_ms=0, end_ms=500, language="zh"),
    STTEvent(kind="final", text="你好。", start_ms=0, end_ms=800, language="zh"),
)


class FakeSTT(_FakeBase):
    """每收到 frames_per_event 帧音频输出下一个预设事件，音频结束后输出剩余事件。

    用量：收到的 PCM16 字节数 / 2 / sample_rate 秒。
    """

    def __init__(
        self,
        events: Sequence[STTEvent] = DEFAULT_STT_EVENTS,
        *,
        provider: str = "fake",
        model: str = "fake-stt",
        frames_per_event: int = 1,
        errors: Iterable[ProviderError | None] = (),
    ) -> None:
        super().__init__(provider, model, errors)
        self._events = tuple(events)
        self._frames_per_event = frames_per_event

    def stream(
        self,
        audio: AsyncIterable[bytes],
        *,
        sample_rate: int = 16000,
        language_hint: str | None = None,
        hotword_list_id: str | None = None,
    ) -> STTStream:
        self.calls.append(
            {
                "sample_rate": sample_rate,
                "language_hint": language_hint,
                "hotword_list_id": hotword_list_id,
            }
        )

        async def body(stream: GeneratorStream[STTEvent, STTUsage]) -> AsyncGenerator[STTEvent]:
            self._raise_scripted()
            pending = deque(self._events)
            received = frames = 0
            async for frame in audio:
                stream.mark_start()
                received += len(frame)
                frames += 1
                stream.usage = STTUsage(audio_seconds=received / 2 / sample_rate)
                if pending and frames % self._frames_per_event == 0:
                    stream.mark_first()
                    yield pending.popleft()
            stream.usage = STTUsage(audio_seconds=received / 2 / sample_rate)
            while pending:
                stream.mark_first()
                yield pending.popleft()

        return GeneratorStream(body)


class FakeTTS(_FakeBase):
    """每个字生成 samples_per_char 个相同的样本（值由字决定），再按 frame_ms 切帧输出。

    用量：已收到的文本字数（取消时也计入）。被 aclose() 或任务取消打断时 cancelled 加一。
    """

    def __init__(
        self,
        *,
        provider: str = "fake",
        model: str = "fake-tts",
        samples_per_char: int = 160,
        frame_ms: int = 20,
        frame_delay_s: float = 0.0,
        errors: Iterable[ProviderError | None] = (),
    ) -> None:
        super().__init__(provider, model, errors)
        self._samples_per_char = samples_per_char
        self._frame_ms = frame_ms
        self._frame_delay_s = frame_delay_s
        self.cancelled = 0

    def synthesize(
        self,
        text: AsyncIterable[str],
        *,
        voice: str | None = None,
        language: str | None = None,
        sample_rate: int = 24000,
    ) -> TTSStream:
        self.calls.append({"voice": voice, "language": language, "sample_rate": sample_rate})
        frame_bytes = sample_rate * self._frame_ms // 1000 * 2

        async def body(stream: GeneratorStream[bytes, TTSUsage]) -> AsyncGenerator[bytes]:
            self._raise_scripted()
            buffer = b""
            characters = 0
            try:
                async for chunk in text:
                    stream.mark_start()
                    characters += len(chunk)
                    stream.usage = TTSUsage(characters=characters)
                    buffer += b"".join(self.pcm_for(char) for char in chunk)
                    while len(buffer) >= frame_bytes:
                        frame, buffer = buffer[:frame_bytes], buffer[frame_bytes:]
                        await asyncio.sleep(self._frame_delay_s)
                        stream.mark_first()
                        yield frame
                if buffer:
                    stream.mark_first()
                    yield buffer
            except (GeneratorExit, asyncio.CancelledError):
                self.cancelled += 1
                raise

        return GeneratorStream(body)

    def pcm_for(self, char: str) -> bytes:
        """某个字对应的音频（测试用来拼出期望输出）。"""
        return struct.pack("<h", ord(char) % 32768) * self._samples_per_char
