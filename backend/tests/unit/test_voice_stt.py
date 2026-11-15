"""LiveKit 识别插件：用 FakeSTT 驱动，检查重采样、事件顺序、语种兜底、用量和错误重试。"""

from __future__ import annotations

import asyncio
import json
import time
import wave
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from livekit import rtc
from livekit.agents import APIConnectOptions, APIError, stt

from interview.config import TurnSettings
from interview.db.models import Language
from interview.providers.base import (
    AuthError,
    BadResponse,
    GeneratorStream,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
    STTEvent,
    STTUsage,
)
from interview.providers.fake import FakeSTT
from interview.voice import stt_plugin
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


async def test_timestamps_are_shifted_by_stream_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    # 基类按首帧的采集时刻定锚：一次推入很多帧会被当成更早采集的音频，所以这里只推一帧
    monkeypatch.setattr(stt_plugin, "SPEECH_MIN_S", 0.0)  # 一帧也算开口
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
        Kind.RECOGNITION_USAGE,  # 没有最终结果的音频也计费
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


async def test_flush_finalizes_at_once_and_next_audio_starts_a_new_task() -> None:
    # VAD 判定候选人停下时 AgentSession 调用 flush()：当前任务马上结束并给出最终结果（C8）
    fake = FakeSTT(EVENTS[2:3], frames_per_event=1000)  # 只在音频结束时出 final
    plugin = ProviderSTT(fake, language=Language.ZH)
    stream = plugin.stream(conn_options=FAST)
    for _ in range(10):
        stream.push_frame(frame())
    stream.flush()
    first: list[stt.SpeechEvent] = []
    async with asyncio.timeout(2):  # 不等后面的音频就拿到最终结果
        while not any(e.type == Kind.FINAL_TRANSCRIPT for e in first):
            first.append(await anext(stream))
    for _ in range(20):
        stream.push_frame(frame())
    stream.end_input()
    rest = [event async for event in stream]
    await stream.aclose()

    assert len(fake.calls) == 2  # 两段话，两个厂商任务
    finals = [e for e in [*first, *rest] if e.type == Kind.FINAL_TRANSCRIPT]
    assert len(finals) == 2
    usage = [e.recognition_usage for e in [*first, *rest] if e.type == Kind.RECOGNITION_USAGE]
    durations = [u.audio_duration for u in usage if u is not None]
    # 每个任务各自计费：第一段 0.1 秒、第二段 0.2 秒，不重复也不遗漏
    assert len(durations) == 2
    assert durations == pytest.approx([0.1, 0.2], abs=0.02)


class StuckSTT(FakeSTT):
    """出一个中间结果后，音频结束（flush）也不给最终结果：卡住或报错。"""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(provider="bailian", model="fun-asr-realtime")
        self.fail = fail

    def stream(self, audio: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        async def body(stream: GeneratorStream[STTEvent, STTUsage]) -> AsyncGenerator[STTEvent]:
            sent = False
            async for _ in audio:
                if not sent:
                    sent = True
                    yield STTEvent(
                        kind="partial", text="I built an order system", start_ms=0, end_ms=900
                    )
            if self.fail:
                raise ProviderTimeout("bailian/fun-asr-realtime: 发出 finish-task 后等待结果超时")
            await asyncio.sleep(30)  # 厂商迟迟不返回最终结果
            yield STTEvent(kind="final", text="too late", start_ms=0, end_ms=900)

        return GeneratorStream(body)


async def first_final(stream: Any) -> stt.SpeechEvent:
    async with asyncio.timeout(5):
        while True:
            event = await anext(stream)
            if event.type == Kind.FINAL_TRANSCRIPT:
                return event


async def test_flush_promotes_partial_when_final_is_late(monkeypatch: pytest.MonkeyPatch) -> None:
    # PR-30 实测：长回答 flush 后 Fun-ASR 十几秒不返回，判定说完被拖到 6–10 秒
    monkeypatch.setattr(stt_plugin, "FLUSH_FINAL_WAIT_S", 0.2)
    stream = ProviderSTT(StuckSTT(), language=Language.EN).stream(conn_options=FAST)
    for _ in range(5):
        stream.push_frame(frame())
    started = asyncio.get_running_loop().time()
    stream.flush()
    final = await first_final(stream)
    assert final.alternatives[0].text == "I built an order system"
    assert asyncio.get_running_loop().time() - started < 2  # 不等厂商的 10 秒超时
    await stream.aclose()


async def test_provider_error_keeps_what_was_heard() -> None:
    # 任务失败时不丢话：先把中间结果当最终结果交出去，再让 LiveKit 重连
    stream = ProviderSTT(StuckSTT(fail=True), language=Language.EN).stream(conn_options=FAST)
    for _ in range(5):
        stream.push_frame(frame())
    stream.flush()
    final = await first_final(stream)
    assert final.alternatives[0].text == "I built an order system"
    await stream.aclose()


def test_flush_wait_is_shorter_than_the_shortest_endpointing_delay() -> None:
    # 这一轮已有文字时 LiveKit 停顿 min_delay 就结束这一轮、不等 flush 的结果；
    # 交出得比它晚，这段话就会被算进下一轮，面试官总在回应上一轮（PR-30 实测）

    turn = TurnSettings.model_validate({})
    assert turn.short_min_delay_s > stt_plugin.FLUSH_FINAL_WAIT_S


async def test_debug_dir_saves_audio_and_event_timeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 排查用（LIVEKIT_DEBUG_AUDIO_DIR）：每段送给识别的音频存成 wav，识别事件按时间记进 jsonl
    monkeypatch.setattr(stt_plugin, "FLUSH_FINAL_WAIT_S", 0.2)
    plugin = ProviderSTT(StuckSTT(), language=Language.EN, debug_dir=tmp_path / "debug-audio")
    stream = plugin.stream(conn_options=FAST)
    for _ in range(5):
        stream.push_frame(frame())
    stream.flush()
    await first_final(stream)
    timeline = await asyncio.wait_for(asyncio.to_thread(saved_timeline, tmp_path), 5)
    await stream.aclose()

    lines = [json.loads(line) for line in timeline.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["audio_s"] > 0
    kinds = [line["kind"] for line in lines[1:]]
    assert sorted(kinds[:2]) == ["flush", "partial"]
    assert kinds[-3:] == ["final_wait_expired", "promoted", "final"]
    assert lines[-2]["text"] == "I built an order system"
    with wave.open(str(timeline.with_suffix(".wav")), "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels()) == (SAMPLE_RATE, 1)
        assert audio.getnframes() > 0


def saved_timeline(root: Path) -> Path:
    """等插件在后台线程里写完第一段（wav 先写，jsonl 后写）。"""
    while not (found := list(root.glob("debug-audio/*-001.jsonl"))):
        time.sleep(0.02)
    return found[0]


async def test_no_debug_files_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert await run(FakeSTT(EVENTS, frames_per_event=5))
    assert not await asyncio.to_thread(lambda: list(tmp_path.iterdir()))


class SlowSTT(FakeSTT):
    """短回答：flush 时还没有任何中间结果，最终结果要过一阵才来（录音重放实测 2.6–6.3 秒）。"""

    def __init__(self, delay_s: float) -> None:
        super().__init__(provider="bailian", model="fun-asr-realtime")
        self.delay_s = delay_s

    def stream(self, audio: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        async def body(stream: GeneratorStream[STTEvent, STTUsage]) -> AsyncGenerator[STTEvent]:
            async for _ in audio:
                pass
            await asyncio.sleep(self.delay_s)
            yield STTEvent(kind="final", text="I'm ready.", start_ms=0, end_ms=900)

        return GeneratorStream(body)


async def test_flush_without_any_text_waits_for_the_late_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # C42：没有文字时 LiveKit 不结束这一轮、等晚到的最终结果；0.3 秒就放弃会把整句话丢掉
    monkeypatch.setattr(stt_plugin, "FLUSH_FINAL_WAIT_S", 0.1)
    monkeypatch.setattr(stt_plugin, "FLUSH_EMPTY_WAIT_S", 3.0)
    stream = ProviderSTT(SlowSTT(0.5), language=Language.EN).stream(conn_options=FAST)
    for _ in range(5):
        stream.push_frame(frame())
    stream.flush()
    final = await first_final(stream)
    assert final.alternatives[0].text == "I'm ready."
    await stream.aclose()


async def test_flush_without_any_text_still_gives_up_eventually(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 厂商一直不返回时也不能无限等：到上限就放弃这个任务，下一段音频照常开新任务
    monkeypatch.setattr(stt_plugin, "FLUSH_EMPTY_WAIT_S", 0.2)
    monkeypatch.setattr(stt_plugin, "SPEECH_MIN_S", 0.0)  # flush 后只推了几帧
    provider = SlowSTT(30)
    stream = ProviderSTT(provider, language=Language.EN).stream(conn_options=FAST)
    for _ in range(5):
        stream.push_frame(frame())
    stream.flush()
    for _ in range(5):  # 太少的音频会被重采样攒着，凑不成一帧
        stream.push_frame(frame())
    for _ in range(60):  # 最多等 3 秒
        if len(provider.calls) >= 2:
            break
        await asyncio.sleep(0.05)
    assert len(provider.calls) == 2  # 放弃上一个任务后，新的音频开了新任务
    await stream.aclose()


def quiet_frame() -> rtc.AudioFrame:
    data = (20).to_bytes(2, "little", signed=True) * FRAME_SAMPLES  # 麦克风底噪
    return rtc.AudioFrame(data, INPUT_RATE, 1, FRAME_SAMPLES)


class CountingSTT(FakeSTT):
    """记下每个任务实际收到多少秒音频。"""

    def __init__(self) -> None:
        super().__init__(provider="bailian", model="fun-asr-realtime")
        self.seconds: list[float] = []

    def stream(self, audio: Any, **kwargs: Any) -> Any:
        self.seconds.append(0.0)

        async def counted() -> AsyncGenerator[bytes]:
            async for chunk in audio:
                self.seconds[-1] += len(chunk) / 2 / SAMPLE_RATE
                yield chunk

        return super().stream(counted(), **kwargs)


async def test_leading_silence_is_not_sent() -> None:
    # C42：Fun-ASR 大致按实时速度处理，段首 2 秒静音会让结果晚 2 秒，还按时长计费
    provider = CountingSTT()
    stream = ProviderSTT(provider, language=Language.EN).stream(conn_options=FAST)
    for _ in range(200):  # 2 秒静音
        stream.push_frame(quiet_frame())
    for _ in range(50):  # 0.5 秒说话
        stream.push_frame(frame())
    stream.flush()
    await first_final(stream)
    await stream.aclose()
    assert len(provider.seconds) == 1
    sent = provider.seconds[0]
    # 说话 0.5 秒 + 预录 PREROLL_S（按帧取整，重采样的帧较大，再放宽 0.1 秒）
    assert 0.5 <= sent <= 0.5 + stt_plugin.PREROLL_S + 0.1


async def test_silence_alone_never_opens_a_task() -> None:
    provider = CountingSTT()
    stream = ProviderSTT(provider, language=Language.EN).stream(conn_options=FAST)
    for _ in range(100):
        stream.push_frame(quiet_frame())
    stream.flush()
    stream.end_input()
    async with asyncio.timeout(3):
        async for _ in stream:
            pass
    assert provider.seconds == []
