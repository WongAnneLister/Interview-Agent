"""真实调用百炼流式识别（make test-live）：读取 fixtures/sample_zh_en.wav，按实时节奏发送。

打印最终文本、首包耗时，以及“服务端计费时长 vs 实际发送的音频时长”，用来确认计费口径。
录音要求：16 kHz、单声道、16 bit PCM 的 wav（不提交到仓库）；文件不存在或未配置时跳过。
"""

from __future__ import annotations

import asyncio
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from interview.config import DEFAULT_ENV_FILE, BailianSettings, STTSettings
from interview.providers.registry import ProviderNotConfigured, bailian_asr_factory

SAMPLE = Path(__file__).parent / "fixtures" / "sample_zh_en.wav"
CHUNK_MS = 100


def read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
            pytest.fail(
                "录音需要 16 kHz、单声道、16 bit：可用 ffmpeg -ar 16000 -ac 1 -sample_fmt s16 转换"
            )
        return wav.readframes(wav.getnframes())


async def realtime(pcm: bytes) -> AsyncIterator[bytes]:
    size = 16000 * 2 * CHUNK_MS // 1000
    for start in range(0, len(pcm), size):
        yield pcm[start : start + size]
        await asyncio.sleep(CHUNK_MS / 1000)


@pytest.mark.live
async def test_stream_sample(capsys: pytest.CaptureFixture[str]) -> None:
    if not SAMPLE.exists():
        pytest.skip(f"缺少录音 {SAMPLE}")
    role = STTSettings(_env_file=DEFAULT_ENV_FILE)  # pyright: ignore[reportCallIssue]
    vendor = BailianSettings(_env_file=DEFAULT_ENV_FILE)  # pyright: ignore[reportCallIssue]
    if role.provider != "bailian" or not role.model:
        pytest.skip("STT_PROVIDER=bailian / STT_MODEL 未配置")
    try:
        asr = bailian_asr_factory(vendor)(role)
    except ProviderNotConfigured as exc:
        pytest.skip(str(exc))

    pcm = read_pcm(SAMPLE)
    try:
        with capture_logs() as logs:
            async with asr.stream(realtime(pcm)) as stream:
                events = [e async for e in stream]
            # 第二个任务复用同一个连接
            async with asr.stream(realtime(pcm[: 16000 * 2])) as second:
                _ = [e async for e in second]
    finally:
        await asr.aclose()

    finals = [e for e in events if e.kind == "final"]
    assert finals, "没有最终识别结果"
    assert stream.usage is not None
    task_ends = [e for e in logs if e["event"] == "asr_task_end"]
    with capsys.disabled():
        partials = len(events) - len(finals)
        print(f"\n[stt] {role.model} 首包 {stream.first_latency_ms:.0f} ms，中间结果 {partials} 条")
        for e in finals:
            print(f"  [{e.start_ms}–{e.end_ms} ms] {e.language or '-'} {e.text}")
        print(f"  计费时长 {stream.usage.audio_seconds} 秒，实际发送 {len(pcm) / 32000:.2f} 秒")
        print(f"  每个最终结果的 usage.duration：{task_ends[0]['durations']}")
        print(f"  第二个任务：{task_ends[1]}")
