"""百炼流式语音合成（CosyVoice / Qwen-Audio-TTS 的 run-task 双工协议），直接用 websockets 实现。

协议（https://help.aliyun.com/zh/model-studio/cosyvoice-client-events ）：
run-task → 等 task-started → 多次 continue-task（文本片段）→ finish-task
→ 收二进制音频帧与 result-generated … → task-finished。服务端自动分句，整句立即合成。

- 一次 synthesize() 是一个任务：边收文本片段边发 continue-task（空片段跳过），
  文本结束发 finish-task；
  音频帧原样输出（PCM16 单声道，采样率由调用参数决定，16000 或 24000）。
- run-task 参数：text_type=PlainText、format=pcm、sample_rate、
  voice（调用参数优先，否则 TTS_VOICE），
  合并 TTS_EXTRA_BODY（如 rate、volume），调用时给了 language 再加 language_hints。
- 两次 continue-task 间隔不能超过 23 秒（服务端限制），上游大模型卡住时服务端会报超时。
- 取消（打断）：调用方 aclose() 或任务被取消时立即停止输出，发 finish-task（directive=cancel），
  丢弃剩余音频等到 task-finished（最多 CANCEL_DRAIN_MAX_S），连接放回空闲槽复用；等不到则关闭连接。
- 首包耗时：发出第一个 continue-task 到收到第一块音频。
- 计费字符：服务端 usage.characters 是累计值（sentence-end 与 task-finished 返回），取最大值；
  任务没有正常结束时与按已发送文本的估计值取较大值（汉字计 2 个字符、其他计 1 个，已实测）。
- 连接复用、预热、task-started 之前重连一次、错误映射：见 providers/dashscope_ws.py。
- 日志只记录 task_id、字符数，不记录文本。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog
from websockets.asyncio.client import ClientConnection

from interview.providers.base import GeneratorStream, TTSStream, TTSUsage
from interview.providers.dashscope_ws import DashScopeTaskClient, command

CANCEL_DRAIN_MAX_S = 2.0

log = structlog.get_logger(__name__)


# 汉字（CJK 统一表意文字及扩展区）的码位范围
_HAN_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x323AF))


def _is_han(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _HAN_RANGES)


def estimate_characters(text: str) -> int:
    """按已发送文本估计计费字符数：汉字按 2 个，其他（英文、数字、中英文标点）按 1 个。

    2026-10-17 实测 cosyvoice-v3-flash：41 字的中文句子（37 个汉字 + 4 个全角标点）计 78，
    83 个字符的英文句子计 83。
    """
    return sum(2 if _is_han(char) else 1 for char in text)


@dataclass
class _TaskState:
    sent: list[str] = field(default_factory=list[str])
    reported: int | None = None  # 服务端返回的累计计费字符数

    def track(self, payload: dict[str, Any]) -> None:
        usage: dict[str, Any] = payload.get("usage") or {}
        characters = usage.get("characters")
        if characters is not None:
            self.reported = max(self.reported or 0, int(characters))

    def characters(self, *, finished: bool) -> int:
        estimate = estimate_characters("".join(self.sent))
        if finished and self.reported is not None:
            return self.reported
        return max(self.reported or 0, estimate)


class BailianTTS:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        url: str,
        timeout_s: float,
        voice: str = "",
        extra_params: Mapping[str, Any] | None = None,
        provider: str = "bailian",
    ) -> None:
        self.provider = provider
        self.model = model
        self._voice = voice
        self._extra_params = dict(extra_params or {})
        self._ws = DashScopeTaskClient(
            name="tts",
            provider=provider,
            model=model,
            api_key=api_key,
            url=url,
            timeout_s=timeout_s,
        )

    def synthesize(
        self,
        text: AsyncIterable[str],
        *,
        voice: str | None = None,
        language: str | None = None,
        sample_rate: int = 24000,
    ) -> TTSStream:
        chosen = voice or self._voice
        if not chosen:
            raise ValueError("未指定音色：调用时传 voice 或配置 TTS_VOICE")
        params: dict[str, Any] = {"text_type": "PlainText", "format": "pcm"}
        params.update(self._extra_params)
        params.update(voice=chosen, sample_rate=sample_rate)
        if language:
            params["language_hints"] = [language]
        return GeneratorStream(
            lambda stream: self._ws.guarded(self._run(stream, text, params, _TaskState()))
        )

    async def warmup(self) -> None:
        await self._ws.warmup()

    async def aclose(self) -> None:
        await self._ws.aclose()

    async def _run(
        self,
        stream: GeneratorStream[bytes, TTSUsage],
        text: AsyncIterable[str],
        params: dict[str, Any],
        state: _TaskState,
    ) -> AsyncGenerator[bytes]:
        task_id = uuid.uuid4().hex
        conn = await self._ws.start_task(
            task_id,
            {
                "task_group": "audio",
                "task": "tts",
                "function": "SpeechSynthesizer",
                "model": self.model,
                "parameters": params,
                "input": {},
            },
        )

        async def send_text() -> None:
            async for chunk in text:
                if not chunk:
                    continue
                stream.mark_start()
                await self._ws.send(
                    conn, command("continue-task", task_id, {"input": {"text": chunk}})
                )
                state.sent.append(chunk)
            await self._ws.send(conn, command("finish-task", task_id, {"input": {}}))

        sender = asyncio.create_task(send_text())
        finished = cancelled = False
        try:
            while True:
                data = await self._ws.next_raw(conn, sender)
                if isinstance(data, bytes):
                    if data:
                        stream.mark_first()
                        yield data
                    continue
                header, payload = self._ws.parse(data)
                state.track(payload)
                event = header.get("event")
                if event == "task-finished":
                    finished = True
                    return
                if event == "task-failed":
                    raise self._ws.task_error(header)
        except (GeneratorExit, asyncio.CancelledError):
            cancelled = True
            raise
        finally:
            if not sender.done():
                sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            reusable = finished or (cancelled and await self._cancel(conn, task_id, state))
            stream.usage = TTSUsage(characters=state.characters(finished=finished))
            log.info(
                "tts_task_end",
                task_id=task_id,
                completed=finished,
                cancelled=cancelled,
                characters=stream.usage.characters,
                reported=state.reported,
                estimated=estimate_characters("".join(state.sent)),
                sent_chunks=len(state.sent),
                first_latency_ms=stream.first_latency_ms,
            )
            if reusable:
                await self._ws.release(conn)
            else:
                await self._ws.discard(conn)

    async def _cancel(self, conn: ClientConnection, task_id: str, state: _TaskState) -> bool:
        """通知服务端取消任务并丢弃剩余音频；收到 task-finished 返回 True（连接可复用）。"""
        try:
            async with asyncio.timeout(min(self._ws.timeout_s, CANCEL_DRAIN_MAX_S)):
                cancel = command("finish-task", task_id, {"input": {"directive": "cancel"}})
                await conn.send(cancel)
                while True:
                    data = await conn.recv()
                    if isinstance(data, bytes):
                        continue
                    header, payload = self._ws.parse(data)
                    state.track(payload)
                    if header.get("event") == "task-finished":
                        return True
                    if header.get("event") == "task-failed":
                        return False
        except Exception:
            log.warning("tts_cancel_failed", task_id=task_id)
            return False
