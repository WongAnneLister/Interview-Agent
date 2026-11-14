"""LiveKit 合成插件：用 FakeTTS 驱动，检查分句与口语规范化、单个 segment、指标、打断和错误重试。"""

from __future__ import annotations

import asyncio

import pytest
from livekit.agents import APIConnectOptions, APIError, tts
from livekit.agents.metrics import TTSMetrics

from interview.db.models import Language
from interview.domain.speech_text import normalize
from interview.providers.base import AuthError, ProviderUnavailable
from interview.providers.fake import FakeTTS
from interview.voice.tts_plugin import SAMPLE_RATE, ProviderTTS

FAST = APIConnectOptions(max_retry=1, retry_interval=0, timeout=5)


def expected_pcm(fake: FakeTTS, *texts: str) -> bytes:
    return b"".join(fake.pcm_for(char) for text in texts for char in text)


async def speak(plugin: ProviderTTS, *chunks: str) -> tuple[list[tts.SynthesizedAudio], bytes]:
    stream = plugin.stream(conn_options=FAST)
    for chunk in chunks:
        stream.push_text(chunk)
    stream.end_input()
    frames = [frame async for frame in stream]
    await stream.aclose()
    return frames, b"".join(bytes(f.frame.data) for f in frames)


async def test_sentences_are_normalized_before_synthesis() -> None:
    fake = FakeTTS()
    plugin = ProviderTTS(fake, language=Language.ZH, voice="Ryan")
    # 大模型的片段会把一个单位拆开（“3m” + “s”），整句规范化才能读成“3毫秒”
    frames, pcm = await speak(plugin, "你好，延迟3m", "s左右。第二", "句")
    first, second = normalize("你好，延迟3ms左右。", Language.ZH), "第二句"
    assert "毫秒" in first
    assert pcm == expected_pcm(fake, first, second)
    assert fake.calls == [{"voice": "Ryan", "language": "zh", "sample_rate": SAMPLE_RATE}]
    assert {f.frame.sample_rate for f in frames} == {SAMPLE_RATE}
    assert len({f.segment_id for f in frames}) == 1  # 整段话只有一个 segment
    assert frames[-1].is_final


async def test_english_sentences_keep_a_space_between_them() -> None:
    fake = FakeTTS()
    _, pcm = await speak(ProviderTTS(fake, language=Language.EN), "Hi. How are", " you?")
    assert pcm == expected_pcm(fake, "Hi. ", "How are you? ")


async def test_metrics_report_ttfb_and_characters() -> None:
    plugin = ProviderTTS(FakeTTS(), language=Language.ZH)
    collected: list[TTSMetrics] = []
    plugin.on("metrics_collected", collected.append)
    await speak(plugin, "你好。")
    await asyncio.sleep(0)
    assert len(collected) == 1
    assert collected[0].ttfb >= 0
    assert collected[0].characters_count == len("你好。")


async def test_close_mid_speech_cancels_provider_stream() -> None:
    fake = FakeTTS(frame_delay_s=0.05)
    stream = ProviderTTS(fake, language=Language.ZH).stream(conn_options=FAST)
    stream.push_text("这是一段很长的回答，" * 5)
    stream.end_input()
    await anext(stream)
    await stream.aclose()  # 候选人打断
    assert fake.cancelled == 1


async def test_unavailable_provider_is_retried_before_any_audio() -> None:
    fake = FakeTTS(errors=[ProviderUnavailable("bailian/tts: 连接失败"), None])
    _, pcm = await speak(ProviderTTS(fake, language=Language.ZH), "你好。")
    assert len(fake.calls) == 2
    assert pcm == expected_pcm(fake, "你好。")  # 重试时基类重放了已推入的文本


async def test_auth_error_is_not_retried() -> None:
    fake = FakeTTS(errors=[AuthError("bailian/tts: HTTP 401")])
    with pytest.raises(APIError) as info:
        await speak(ProviderTTS(fake, language=Language.ZH), "你好。")
    assert info.value.retryable is False
    assert len(fake.calls) == 1


async def test_synthesize_uses_the_same_stream() -> None:
    fake = FakeTTS()
    plugin = ProviderTTS(fake, language=Language.ZH)
    async with plugin.synthesize("吞吐提升了30%。", conn_options=FAST) as chunked:
        pcm = b"".join([bytes(f.frame.data) async for f in chunked])
    expected = expected_pcm(fake, normalize("吞吐提升了30%。", Language.ZH))
    # 基类把流式结果转成整段时会在末尾补几毫秒静音
    assert pcm.startswith(expected)
    assert set(pcm[len(expected) :]) <= {0}


async def test_prewarm_warms_provider_and_identifies_it() -> None:
    fake = FakeTTS(provider="bailian", model="qwen3-tts-flash-realtime")
    plugin = ProviderTTS(fake, language=Language.ZH)
    plugin.prewarm()
    await asyncio.sleep(0)
    assert fake.warmups == 1
    assert (plugin.provider, plugin.model) == ("bailian", "qwen3-tts-flash-realtime")
    await plugin.aclose()
