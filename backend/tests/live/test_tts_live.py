"""真实调用百炼流式合成（make test-live）。

四个候选音色各合成一句中文、一句英文，写入 tests/live/out/。

- 先 warmup 再合成，文本按大模型的节奏分段送入；打印首包耗时，以及“服务端计费字符 vs 文本字数”，
  用来确认汉字计几个字符。
- 再合成一段长文本并中途取消，接着在同一个连接上合成下一句，验证取消后连接可复用。
需要 BAILIAN_API_KEY、TTS_MODEL、TTS_BASE_URL；未配置时跳过。输出目录不提交。
"""

from __future__ import annotations

import asyncio
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from interview.config import DEFAULT_ENV_FILE, BailianSettings, TTSSettings
from interview.providers.bailian_tts import BailianTTS

OUT = Path(__file__).parent / "out"
VOICES = ["longanlang_v3", "longshuo_v3", "longanwen_v3", "longxiaoxia_v3"]
SAMPLE_RATE = 24000
SENTENCES = {
    "zh": "你好，我是今天的面试官。先请你用两分钟介绍一下最近做的项目，重点说说你负责的部分。",
    "en": "Great. Could you walk me through how you designed the retry logic for that service?",
}


def make_client(voice: str) -> BailianTTS:
    role = TTSSettings(_env_file=DEFAULT_ENV_FILE)  # pyright: ignore[reportCallIssue]
    vendor = BailianSettings(_env_file=DEFAULT_ENV_FILE)  # pyright: ignore[reportCallIssue]
    if role.provider != "bailian" or not role.model or not role.base_url or not vendor.configured:
        pytest.skip("BAILIAN_API_KEY / TTS_PROVIDER=bailian / TTS_MODEL / TTS_BASE_URL 未配置")
    return BailianTTS(
        model=role.model,
        api_key=vendor.api_key.get_secret_value(),
        url=role.base_url,
        timeout_s=role.timeout_s,
        voice=voice,
        extra_params=role.extra_body,
    )


async def like_llm(text: str, *, step: int = 6, delay_s: float = 0.05) -> AsyncIterator[str]:
    """按大模型流式输出的节奏分段送入文本。"""
    for start in range(0, len(text), step):
        yield text[start : start + step]
        await asyncio.sleep(delay_s)


def write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)


@pytest.mark.live
@pytest.mark.parametrize("voice", VOICES)
async def test_synthesize_voices(voice: str, capsys: pytest.CaptureFixture[str]) -> None:
    client = make_client(voice)
    lines: list[str] = []
    try:
        await client.warmup()
        for language, text in SENTENCES.items():
            with capture_logs() as logs:
                async with client.synthesize(like_llm(text), sample_rate=SAMPLE_RATE) as stream:
                    pcm = b"".join([frame async for frame in stream])
            assert pcm, "没有收到音频"
            path = OUT / f"tts_{voice}_{language}.wav"
            write_wav(path, pcm)
            (end,) = [e for e in logs if e["event"] == "tts_task_end"]
            lines.append(
                f"  {language} 首包 {stream.first_latency_ms:.0f} ms，"
                f"音频 {len(pcm) / 2 / SAMPLE_RATE:.1f} 秒，服务端字符 {end['reported']}，"
                f"文本 {len(text)} 字（估计 {end['estimated']}）→ {path.name}"
            )
    finally:
        await client.aclose()
    with capsys.disabled():
        print(f"\n[tts] {voice}", *lines, sep="\n")


@pytest.mark.live
async def test_cancel_then_reuse(capsys: pytest.CaptureFixture[str]) -> None:
    client = make_client(VOICES[0])
    long_text = SENTENCES["zh"] * 4
    try:
        await client.warmup()
        with capture_logs() as logs:
            stream = client.synthesize(like_llm(long_text), sample_rate=SAMPLE_RATE)
            first = await anext(stream)
            await stream.aclose()  # 模拟候选人打断
            async with client.synthesize(like_llm("好的，我们继续。")) as after:
                pcm = b"".join([frame async for frame in after])
    finally:
        await client.aclose()

    events = [e["event"] for e in logs]
    cancelled, resumed = [e for e in logs if e["event"] == "tts_task_end"]
    assert first and pcm
    assert cancelled["cancelled"] and not cancelled["completed"]
    assert resumed["completed"]
    assert "tts_cancel_failed" not in events
    assert "tts_reconnect" not in events
    with capsys.disabled():
        print(
            f"\n[tts] 取消：服务端字符 {cancelled['reported']}，计费按 {cancelled['characters']}；"
            f"取消后下一句首包 {after.first_latency_ms:.0f} ms（复用同一连接）"
        )
