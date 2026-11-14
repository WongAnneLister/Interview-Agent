"""排查用：把每段送给识别的音频存成 wav，并记下这段的识别事件时间线（默认关闭）。

用途：分清“声音本身就坏了”（采集、CPU、编码）和“识别服务没断句 / 没出结果”。
LIVEKIT_DEBUG_AUDIO_DIR 设了目录才启用；文件只在本机，包含候选人原话，不要提交、不要外传。
每段两个文件：<时间>-<序号>.wav（16 kHz 单声道 PCM16）和 .jsonl（t 是距这段开始的秒数）。
写文件放到线程里，不卡语音的事件循环。
"""

from __future__ import annotations

import asyncio
import json
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any


class SegmentRecorder:
    def __init__(self, directory: Path, index: int, *, sample_rate: int) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._base = directory / f"{stamp}-{index:03d}"
        self._sample_rate = sample_rate
        self._audio = bytearray()
        self._events: list[dict[str, Any]] = []
        self._t0 = time.monotonic()

    def audio(self, chunk: bytes) -> None:
        self._audio += chunk

    def event(self, kind: str, **fields: Any) -> None:
        self._events.append({"t": round(time.monotonic() - self._t0, 3), "kind": kind, **fields})

    async def save(self) -> None:
        await asyncio.to_thread(self._write)

    def _write(self) -> None:
        self._base.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self._base.with_suffix(".wav")), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(self._sample_rate)
            out.writeframes(bytes(self._audio))
        seconds = len(self._audio) / 2 / self._sample_rate
        lines = [json.dumps({"audio_s": round(seconds, 2)}, ensure_ascii=False)]
        lines += [json.dumps(event, ensure_ascii=False) for event in self._events]
        self._base.with_suffix(".jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
