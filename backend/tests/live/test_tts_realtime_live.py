"""真实调用 Qwen-TTS Realtime（make test-live）。

Ryan、Bodega 各按几种语速（speech_rate）合成一句中文、一句英文，写入 tests/live/out/，
文件名带语速，如 tts_Ryan_zh_x1.2.wav，方便对比试听。

打印首包耗时、音频时长、会话累计字符与本次计费字符（对照文本估计值）。
只需要 BAILIAN_API_KEY（与 .env 里当前选用的 TTS_PROVIDER 无关）；型号和地址取官方文档的值。
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs
from test_tts_live import OUT, SAMPLE_RATE, SENTENCES, like_llm, write_wav

from interview.config import DEFAULT_ENV_FILE, BailianSettings
from interview.providers.bailian_tts import estimate_characters
from interview.providers.bailian_tts_realtime import BailianTTSRealtime

MODEL = "qwen3-tts-flash-realtime"
URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
VOICES = ["Ryan", "Bodega"]
SPEECH_RATES = [1.0, 1.2, 1.3]


@pytest.mark.live
@pytest.mark.parametrize("voice", VOICES)
async def test_realtime_voices(voice: str, capsys: pytest.CaptureFixture[str]) -> None:
    vendor = BailianSettings(_env_file=DEFAULT_ENV_FILE)  # pyright: ignore[reportCallIssue]
    if not vendor.configured:
        pytest.skip("BAILIAN_API_KEY 未配置")
    lines: list[str] = []
    for rate in SPEECH_RATES:
        tts = BailianTTSRealtime(
            model=MODEL,
            api_key=vendor.api_key.get_secret_value(),
            url=URL,
            timeout_s=10,
            voice=voice,
            extra_params={"speech_rate": rate},
        )
        try:
            await tts.warmup()
            for language, text in SENTENCES.items():
                with capture_logs() as logs:
                    async with tts.synthesize(like_llm(text), sample_rate=SAMPLE_RATE) as stream:
                        pcm = b"".join([frame async for frame in stream])
                assert pcm, "没有收到音频"
                path = OUT / f"tts_{voice}_{language}_x{rate}.wav"
                write_wav(path, pcm)
                (end,) = [e for e in logs if e["event"] == "tts_realtime_end"]
                lines.append(
                    f"  x{rate} {language} 首包 {stream.first_latency_ms:.0f} ms，"
                    f"音频 {len(pcm) / 2 / SAMPLE_RATE:.1f} 秒，"
                    f"会话累计 {end['responses']}，本次计费 {end['characters']}，"
                    f"文本 {len(text)} 字（估计 {estimate_characters(text)}）→ {path.name}"
                )
        finally:
            await tts.aclose()
    with capsys.disabled():
        print(f"\n[tts-realtime] {voice}", *lines, sep="\n")
