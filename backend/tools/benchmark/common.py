"""实测脚本共用：只读取厂商相关配置、按地址推断地域、环境信息、音频与文本的节奏模拟。

实测直接调用适配器，不经过观测包装，不写用量表，不需要隧道。
"""

from __future__ import annotations

import asyncio
import platform
import subprocess
import wave
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from interview.config import (
    DEFAULT_ENV_FILE,
    REPO_ROOT,
    BailianSettings,
    DeepSeekSettings,
    LLMEvalSettings,
    LLMFallbackSettings,
    LLMFrontSettings,
    ProvidersSettings,
    STTSettings,
    TTSSettings,
)
from interview.observability.pricing import BEIJING_TZ

BENCHMARK_DIR = Path(__file__).resolve().parent
DATA_DIR = BENCHMARK_DIR / "data"  # 录音、参考文本、术语表（不提交）
OUT_DIR = BENCHMARK_DIR / "out"  # 合成音频（不提交）
REPORT_DIR = REPO_ROOT / "docs" / "benchmarks"


def provider_settings() -> ProvidersSettings:
    """只读取厂商相关配置，不要求数据库、LiveKit、对象存储等必填项。"""

    def read[T](cls: type[T]) -> T:
        return cls(_env_file=DEFAULT_ENV_FILE)  # pyright: ignore[reportCallIssue]

    return ProvidersSettings(
        llm_front=read(LLMFrontSettings),
        llm_eval=read(LLMEvalSettings),
        llm_fallback=read(LLMFallbackSettings),
        stt=read(STTSettings),
        tts=read(TTSSettings),
        bailian=read(BailianSettings),
        deepseek=read(DeepSeekSettings),
    )


def region(url: str) -> str:
    """按接口地址推断地域。"""
    host = urlparse(url).hostname or ""
    if "dashscope-intl" in host:
        return "新加坡"
    if "cn-beijing" in host or host == "dashscope.aliyuncs.com":
        return "华北2（北京）"
    if host.endswith("deepseek.com"):
        return "DeepSeek 官方"
    return host or "-"


def now() -> datetime:
    return datetime.now(BEIJING_TZ)


def git_commit() -> str:
    try:
        result = subprocess.run(  # 固定参数，不含用户输入
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            cwd=REPO_ROOT,
        )
    except (OSError, subprocess.SubprocessError):
        return "-"
    return result.stdout.strip()


def machine() -> str:
    system = f"{platform.system()} {platform.release()} / {platform.machine()}"
    return f"{system} / Python {platform.python_version()}"


async def like_llm(text: str, *, step: int = 6, delay_s: float = 0.05) -> AsyncIterator[str]:
    """按大模型流式输出的节奏分段送出文本。"""
    for start in range(0, len(text), step):
        yield text[start : start + step]
        await asyncio.sleep(delay_s)


def write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


def read_pcm16_mono_16k(path: Path) -> bytes:
    """读取 16 kHz、单声道、16 bit 的 wav；格式不对时抛 ValueError。"""
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
            raise ValueError(f"{path.name} 不是 16 kHz 单声道 16 bit（见 README 的转换命令）")
        return wav.readframes(wav.getnframes())
