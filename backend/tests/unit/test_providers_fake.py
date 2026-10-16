"""假实现：输出与用量确定、错误队列、首包耗时、TTS 中途取消。"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from interview.providers.base import (
    FAILOVER_ERRORS,
    AuthError,
    ChatMessage,
    LLMProvider,
    LLMUsage,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
    STTEvent,
    STTProvider,
    STTUsage,
    TTSProvider,
    TTSUsage,
)
from interview.providers.fake import FakeLLM, FakeSTT, FakeTTS

MESSAGES: list[ChatMessage] = [
    {"role": "system", "content": "你是面试官"},
    {"role": "user", "content": "你好"},
]


async def frames(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def texts(*chunks: str) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


def test_fakes_satisfy_protocols() -> None:
    llm: LLMProvider = FakeLLM()
    stt: STTProvider = FakeSTT()
    tts: TTSProvider = FakeTTS()
    assert (llm.model, stt.model, tts.model) == ("fake-llm", "fake-stt", "fake-tts")


def test_error_hierarchy() -> None:
    assert issubclass(ProviderTimeout, TimeoutError)
    assert AuthError not in FAILOVER_ERRORS
    error = RateLimited("限流", provider="bailian", model="m")
    assert (error.provider, error.model, str(error)) == ("bailian", "m", "限流")


async def test_llm_stream_chunks_usage_and_latency() -> None:
    llm = FakeLLM("一二三四五六七", chunk_size=3)
    stream = llm.stream_chat(MESSAGES, max_tokens=100, temperature=0.3, extra={"x": 1})
    assert [chunk async for chunk in stream] == ["一二三", "四五六", "七"]
    assert stream.usage == LLMUsage(prompt_tokens=7, completion_tokens=7)
    assert stream.first_latency_ms is not None
    assert llm.calls[0]["max_tokens"] == 100
    assert llm.calls[0]["extra"] == {"x": 1}


async def test_llm_complete_echoes_by_default() -> None:
    result = await FakeLLM().complete(MESSAGES, max_tokens=10, temperature=0)
    assert result.text == "[fake] 你好"
    assert result.usage == LLMUsage(prompt_tokens=7, completion_tokens=9)
    assert result.first_token_ms is not None


async def test_llm_errors_are_consumed_per_call() -> None:
    llm = FakeLLM("好", errors=[ProviderTimeout("超时"), None])
    stream = llm.stream_chat(MESSAGES, max_tokens=10, temperature=0)
    with pytest.raises(ProviderTimeout):
        await anext(stream)
    assert stream.usage is None
    assert (await llm.complete(MESSAGES, max_tokens=10, temperature=0)).text == "好"


async def test_llm_fail_after_chunks_keeps_partial_usage() -> None:
    llm = FakeLLM("abcdef", chunk_size=2, fail_after_chunks=1)
    stream = llm.stream_chat(MESSAGES, max_tokens=10, temperature=0)
    assert await anext(stream) == "ab"
    with pytest.raises(ProviderUnavailable):
        await anext(stream)
    assert stream.usage == LLMUsage(prompt_tokens=7, completion_tokens=2)


async def test_stt_events_follow_audio_and_usage_is_seconds() -> None:
    events = [
        STTEvent(kind="partial", text="hi", start_ms=0, end_ms=100),
        STTEvent(kind="partial", text="hi the", start_ms=0, end_ms=200),
        STTEvent(kind="final", text="hi there", start_ms=0, end_ms=300, language="en"),
    ]
    stt = FakeSTT(events, frames_per_event=2)
    stream = stt.stream(frames(b"\0" * 320, b"\0" * 320, b"\0" * 320), hotword_list_id="v1")
    assert [e async for e in stream] == events
    assert stream.usage == STTUsage(audio_seconds=960 / 2 / 16000)
    assert stream.first_latency_ms is not None
    assert stt.calls == [{"sample_rate": 16000, "language_hint": None, "hotword_list_id": "v1"}]


async def test_tts_frames_are_deterministic() -> None:
    tts = FakeTTS(samples_per_char=200, frame_ms=10)  # 16 kHz：每帧 160 样本 = 320 字节
    stream = tts.synthesize(texts("你", "好"), voice="v", sample_rate=16000)
    audio = [frame async for frame in stream]
    assert b"".join(audio) == tts.pcm_for("你") + tts.pcm_for("好")
    assert [len(frame) for frame in audio] == [320, 320, 160]
    assert stream.usage == TTSUsage(characters=2)
    assert stream.first_latency_ms is not None
    assert tts.cancelled == 0


async def test_tts_aclose_cancels_midway() -> None:
    tts = FakeTTS(frame_ms=10)
    async with tts.synthesize(texts("很长的一句话"), sample_rate=16000) as stream:
        await anext(stream)
    assert tts.cancelled == 1
    assert stream.usage == TTSUsage(characters=6)  # 已收到的文本照常计费
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_tts_task_cancellation_is_counted() -> None:
    tts = FakeTTS(frame_ms=10, frame_delay_s=10)
    stream = tts.synthesize(texts("打断"), sample_rate=16000)
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tts.cancelled == 1


async def test_aclose_marks_provider_closed() -> None:
    llm = FakeLLM()
    await llm.aclose()
    assert llm.closed
