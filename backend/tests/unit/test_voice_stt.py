"""LiveKit 识别插件：用 FakeSTT 驱动，检查重采样、事件顺序、语种兜底、用量和错误重试。"""

from __future__ import annotations

import asyncio

import pytest
from livekit import rtc
from livekit.agents import APIConnectOptions, APIError, stt

from interview.db.models import Language
from interview.providers.base import (
    AuthError,
    BadResponse,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
    STTEvent,
)
from interview.providers.fake import FakeSTT
from interview.voice.plugin_errors import to_api_error
from interview.voice.stt_plugin import SAMPLE_RATE, ProviderSTT

FAST = APIConnectOptions(max_retry=1, retry_interval=0, timeout=5)
INPUT_RATE = 48000
FRAME_SAMPLES = INPUT_RATE // 100  # 10 ms
EVENTS = (
    STTEvent(kind="partial", text="", start_ms=0, end_ms=100),  # 空 partial 不算开口
    STTEvent(kind="partial", text="我做过", start_ms=0, end_ms=600),
    STTEvent(kind="final", text="我做过支付系统。", start_ms=0, end_ms=1200),
    STTEvent(kind="final", text="Redis 做缓存。", start_ms=1500, end_ms=2400, language="en"),
)
Kind = stt.SpeechEventType


def frame() -> rtc.AudioFrame:
    data = (1000).to_bytes(2, "little", signed=True) * FRAME_SAMPLES
    return rtc.AudioFrame(data, INPUT_RATE, 1, FRAME_SAMPLES)


async def run(
    fake: FakeSTT, *, frames: int = 30, offset: float | None = None
) -> list[stt.SpeechEvent]:
    plugin = ProviderSTT(fake, language=Language.ZH, hotword_list_id="vocab-1")
    stream = plugin.stream(conn_options=FAST)
    if offset is not None:
        stream.start_time_offset = offset
    for _ in range(frames):
        stream.push_frame(frame())
    stream.end_input()
    events = [event async for event in stream]
    await stream.aclose()
    return events


async def test_events_follow_livekit_order_with_language_fallback() -> None:
    fake = FakeSTT(EVENTS, frames_per_event=5)
    events = await run(fake)
    assert [e.type for e in events] == [
        Kind.START_OF_SPEECH,
        Kind.INTERIM_TRANSCRIPT,
        Kind.FINAL_TRANSCRIPT,
        Kind.END_OF_SPEECH,
        Kind.RECOGNITION_USAGE,
        Kind.START_OF_SPEECH,
        Kind.FINAL_TRANSCRIPT,
        Kind.END_OF_SPEECH,
    ]
    finals = [e.alternatives[0] for e in events if e.type == Kind.FINAL_TRANSCRIPT]
    assert [(d.text, d.language) for d in finals] == [
        ("我做过支付系统。", "zh"),  # 事件不带语种，用面试语言兜底（C11）
        ("Redis 做缓存。", "en"),
    ]


async def test_audio_is_resampled_to_16k_pcm_and_usage_reported() -> None:
    fake = FakeSTT(EVENTS[2:3], frames_per_event=1000)  # 音频结束后才出 final
    events = await run(fake, frames=30)  # 0.3 秒
    assert fake.calls == [
        {"sample_rate": SAMPLE_RATE, "language_hint": None, "hotword_list_id": "vocab-1"}
    ]
    usage = [e.recognition_usage for e in events if e.type == Kind.RECOGNITION_USAGE]
    assert len(usage) == 1 and usage[0] is not None
    # FakeSTT 按 16 kHz PCM16 字节数算时长：重采样正确时约等于送进去的 0.3 秒
    assert usage[0].audio_duration == pytest.approx(0.3, abs=0.02)


async def test_timestamps_are_shifted_by_stream_offset() -> None:
    # 基类按首帧的采集时刻定锚：一次推入很多帧会被当成更早采集的音频，所以这里只推一帧
    events = await run(FakeSTT(EVENTS[2:3]), frames=1, offset=2.0)
    final = next(e.alternatives[0] for e in events if e.type == Kind.FINAL_TRANSCRIPT)
    assert final.start_time == pytest.approx(2.0, abs=0.05)
    assert final.end_time == pytest.approx(3.2, abs=0.05)


async def test_unfinished_sentence_still_ends_speech() -> None:
    events = await run(FakeSTT(EVENTS[1:2]))
    assert [e.type for e in events] == [
        Kind.START_OF_SPEECH,
        Kind.INTERIM_TRANSCRIPT,
        Kind.END_OF_SPEECH,
    ]


async def test_unavailable_provider_is_retried_by_livekit() -> None:
    fake = FakeSTT(EVENTS[2:3], errors=[ProviderUnavailable("bailian/fun-asr: 连接失败"), None])
    events = await run(fake)
    assert len(fake.calls) == 2
    assert Kind.FINAL_TRANSCRIPT in [e.type for e in events]


async def test_auth_error_is_not_retried() -> None:
    fake = FakeSTT(EVENTS, errors=[AuthError("bailian/fun-asr: HTTP 401")])
    with pytest.raises(APIError) as info:
        await run(fake)
    assert info.value.retryable is False
    assert len(fake.calls) == 1


def test_error_mapping() -> None:
    cases = [
        (ProviderTimeout("t"), True),
        (ProviderUnavailable("u"), True),
        (RateLimited("r"), True),
        (AuthError("a"), False),
        (BadResponse("b"), False),
    ]
    assert [to_api_error(exc).retryable for exc, _ in cases] == [ok for _, ok in cases]
    assert str(to_api_error(AuthError("bailian/x: HTTP 401"))) == "bailian/x: HTTP 401"


async def test_prewarm_warms_provider_and_identifies_it() -> None:
    fake = FakeSTT(provider="bailian", model="fun-asr-realtime")
    plugin = ProviderSTT(fake, language=Language.ZH)
    plugin.prewarm()
    await asyncio.sleep(0)
    assert fake.warmups == 1
    assert (plugin.provider, plugin.model) == ("bailian", "fun-asr-realtime")
    await plugin.aclose()
