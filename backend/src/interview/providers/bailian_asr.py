"""百炼流式语音识别（Fun-ASR 实时，run-task / finish-task WebSocket 协议），直接用 websockets 实现。

协议（https://help.aliyun.com/zh/model-studio/fun-asr-realtime-websocket-api ）：
连接 → run-task → 等 task-started → 发二进制 PCM → finish-task
→ 收 result-generated … → task-finished。
task-finished 后连接可以复用（下一个任务用新的 task_id）；task-failed 后服务端会关闭连接。

- 一次 stream() 是一个任务；audio 结束即发送 finish-task。音频帧原样转发，调用方负责按实时节奏送入。
- run-task 参数：format=pcm、sample_rate，合并配置 STT_EXTRA_BODY（如 language_hints、
  max_sentence_silence），再合并调用参数 language_hint / hotword_list_id。默认不设 language_hints，
  让模型自动识别中英混说。
- 连接复用：保留 1 个空闲连接，空闲超过 IDLE_MAX_S 主动丢弃（服务端约 60 秒无任务会断开）；
  出错、task-failed、调用方中途取消时关闭连接，不复用。
- 重连：只在 task-started 之前连接断开或建连失败时重连一次（此时还没发音频）；任务中途掉线直接抛出。
- 超时（timeout_s）：建连握手、等 task-started、每次发送、发出 finish-task 之后等每条消息。
  说话过程中没有识别事件属于正常情况，不设超时，由服务端的无音频超时兜底。
- 计费时长：按任务内发送的音频时长计费（含静音，不是有效语音时长，也不是连接时长）。
  2026-10-17 实测：每个最终结果的 usage.duration 是截至该句结束的累计音频时长（取整到秒），
  最后一句之后的音频不会再报。所以取 max(最大的 usage.duration, 已发送音频时长)。
- 文本为空的识别结果（噪声、尾音）不输出，用量照常累计。
- 日志只记录 task_id、时长、错误码，不记录识别文本。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterable, Mapping
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from typing import Any, cast

import structlog
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.protocol import State

from interview.providers.base import (
    AuthError,
    BadResponse,
    GeneratorStream,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
    STTEvent,
    STTStream,
    STTUsage,
)

IDLE_MAX_S = 50.0

log = structlog.get_logger(__name__)


@dataclass
class _TaskState:
    sample_rate: int
    sent_bytes: int = 0
    durations: list[float] = field(default_factory=list[float])

    @property
    def sent_seconds(self) -> float:
        return self.sent_bytes / 2 / self.sample_rate

    def billed_seconds(self) -> float:
        return max([*self.durations, self.sent_seconds])


class BailianASR:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        url: str,
        timeout_s: float,
        extra_params: Mapping[str, Any] | None = None,
        provider: str = "bailian",
    ) -> None:
        self.provider = provider
        self.model = model
        self._api_key = api_key
        self._url = url
        self._timeout_s = timeout_s
        self._extra_params = dict(extra_params or {})
        self._idle: tuple[ClientConnection, float] | None = None

    def stream(
        self,
        audio: AsyncIterable[bytes],
        *,
        sample_rate: int = 16000,
        language_hint: str | None = None,
        hotword_list_id: str | None = None,
    ) -> STTStream:
        params: dict[str, Any] = {"format": "pcm", "sample_rate": sample_rate}
        params.update(self._extra_params)
        if language_hint:
            params["language_hints"] = [language_hint]
        if hotword_list_id:
            params["vocabulary_id"] = hotword_list_id

        async def body(stream: GeneratorStream[STTEvent, STTUsage]) -> AsyncGenerator[STTEvent]:
            try:
                # aclosing：外层被 aclose() 时立即关闭内层生成器（释放连接、写入用量），不等垃圾回收
                async with aclosing(
                    self._run(stream, audio, params, _TaskState(sample_rate))
                ) as run:
                    async for event in run:
                        yield event
            except ProviderError:
                raise
            except InvalidStatus as exc:
                raise self._status_error(exc.response.status_code) from exc
            except TimeoutError as exc:
                raise self._error(ProviderTimeout, "连接或发送超时") from exc
            except (ConnectionClosed, OSError) as exc:
                raise self._error(ProviderUnavailable, "连接失败或中断") from exc

        return GeneratorStream(body)

    async def aclose(self) -> None:
        if self._idle is not None:
            conn, _ = self._idle
            self._idle = None
            await self._discard(conn)

    # ---- 一个任务 ----

    async def _run(
        self,
        stream: GeneratorStream[STTEvent, STTUsage],
        audio: AsyncIterable[bytes],
        params: dict[str, Any],
        state: _TaskState,
    ) -> AsyncGenerator[STTEvent]:
        task_id = uuid.uuid4().hex
        conn = await self._start_task(task_id, params)
        sender = asyncio.create_task(self._send_audio(conn, task_id, audio, stream, state))
        reusable = False
        try:
            while True:
                header, payload = await self._next_message(conn, sender)
                event = header.get("event")
                if event == "task-finished":
                    reusable = True
                    return
                if event == "task-failed":
                    raise self._task_error(header)
                if event != "result-generated":
                    continue
                result = self._to_event(payload, state)
                if result is not None:
                    stream.mark_first()
                    yield result
        finally:
            if not sender.done():
                sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            stream.usage = STTUsage(audio_seconds=state.billed_seconds())
            log.info(
                "asr_task_end",
                task_id=task_id,
                completed=reusable,
                billed_s=stream.usage.audio_seconds,
                sent_s=round(state.sent_seconds, 3),
                durations=state.durations,
            )
            if reusable:
                await self._release(conn)
            else:
                await self._discard(conn)

    async def _start_task(self, task_id: str, params: dict[str, Any]) -> ClientConnection:
        command = {
            "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self.model,
                "parameters": params,
                "input": {},
            },
        }
        for attempt in (1, 2):
            conn: ClientConnection | None = None
            try:
                conn = await self._acquire()
                async with asyncio.timeout(self._timeout_s):
                    await conn.send(json.dumps(command))
                    while True:
                        header, _ = self._parse(await conn.recv())
                        if header.get("event") == "task-started":
                            return conn
                        if header.get("event") == "task-failed":
                            raise self._task_error(header)
            except ProviderError:
                await self._discard(conn)
                raise
            except TimeoutError as exc:
                await self._discard(conn)
                raise self._error(ProviderTimeout, "建连或等待 task-started 超时") from exc
            except (ConnectionClosed, OSError) as exc:
                # 还没发音频：空闲连接已被服务端断开、或建连失败，换新连接重试一次
                await self._discard(conn)
                if attempt == 2:
                    raise self._error(ProviderUnavailable, "任务开始前连接失败") from exc
                log.warning("asr_reconnect", task_id=task_id, error=type(exc).__name__)
            except BaseException:
                await self._discard(conn)
                raise
        raise AssertionError("unreachable")

    async def _send_audio(
        self,
        conn: ClientConnection,
        task_id: str,
        audio: AsyncIterable[bytes],
        stream: GeneratorStream[STTEvent, STTUsage],
        state: _TaskState,
    ) -> None:
        async for frame in audio:
            if not frame:
                continue
            stream.mark_start()
            async with asyncio.timeout(self._timeout_s):
                await conn.send(frame)
            state.sent_bytes += len(frame)
        finish: dict[str, Any] = {
            "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {}},
        }
        async with asyncio.timeout(self._timeout_s):
            await conn.send(json.dumps(finish))

    async def _next_message(
        self, conn: ClientConnection, sender: asyncio.Task[None]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """等下一条服务端消息；发送端出错时立即抛出，发完 finish-task 之后每条消息都有超时。"""
        recv = asyncio.ensure_future(conn.recv())
        try:
            while True:
                waiting: set[asyncio.Future[Any]] = {recv} if sender.done() else {recv, sender}
                timeout = self._timeout_s if sender.done() else None
                done, _ = await asyncio.wait(
                    waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if recv in done:
                    return self._parse(recv.result())
                if not done:
                    raise self._error(ProviderTimeout, "发出 finish-task 后等待结果超时")
                sender.result()  # 发送端出错时在这里抛出；正常结束则继续等消息
        finally:
            if not recv.done():
                recv.cancel()  # websockets 保证取消 recv() 不会丢数据

    # ---- 连接管理 ----

    async def _acquire(self) -> ClientConnection:
        if self._idle is not None:
            conn, since = self._idle
            self._idle = None
            if conn.state is State.OPEN and time.monotonic() - since < IDLE_MAX_S:
                return conn
            await self._discard(conn)
        return await connect(
            self._url,
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            open_timeout=self._timeout_s,
            close_timeout=self._timeout_s,
        )

    async def _release(self, conn: ClientConnection) -> None:
        if self._idle is None and conn.state is State.OPEN:
            self._idle = (conn, time.monotonic())
        else:
            await self._discard(conn)

    async def _discard(self, conn: ClientConnection | None) -> None:
        if conn is not None:
            with suppress(Exception):
                await conn.close()

    # ---- 解析与错误 ----

    def _parse(self, data: str | bytes) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            message = json.loads(data)
        except ValueError as exc:
            raise self._error(BadResponse, "服务端消息不是 JSON") from exc
        if not isinstance(message, dict):
            raise self._error(BadResponse, "服务端消息格式不正确")
        body = cast(dict[str, Any], message)
        header: Any = body.get("header")
        payload: Any = body.get("payload") or {}
        if not isinstance(header, dict) or not isinstance(payload, dict):
            raise self._error(BadResponse, "服务端消息缺少 header / payload")
        return cast(dict[str, Any], header), cast(dict[str, Any], payload)

    def _to_event(self, payload: dict[str, Any], state: _TaskState) -> STTEvent | None:
        try:
            sentence: dict[str, Any] = payload["output"]["sentence"]
            if sentence.get("heartbeat"):
                return None
            usage: dict[str, Any] = payload.get("usage") or {}
            duration = usage.get("duration")
            if duration is not None:
                state.durations.append(float(duration))
            text = str(sentence.get("text") or "")
            if not text.strip():
                return None
            begin = int(sentence.get("begin_time") or 0)
            end = sentence.get("end_time")
            if end is None:  # 中间结果的 end_time 可能为空，取最后一个字的结束时间
                words: list[dict[str, Any]] = sentence.get("words") or []
                end = max((int(w.get("end_time") or begin) for w in words), default=begin)
            return STTEvent(
                kind="final" if sentence.get("sentence_end") else "partial",
                text=text,
                start_ms=begin,
                end_ms=int(end),
                language=sentence.get("language") or None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise self._error(BadResponse, "识别结果格式不正确") from exc

    def _error(self, kind: type[ProviderError], message: str) -> ProviderError:
        return kind(
            f"{self.provider}/{self.model}: {message}", provider=self.provider, model=self.model
        )

    def _status_error(self, status: int) -> ProviderError:
        if status in (401, 403):
            return self._error(AuthError, f"握手被拒绝 HTTP {status}（密钥无效或无权限）")
        if status == 429:
            return self._error(RateLimited, "握手被拒绝 HTTP 429")
        if status >= 500:
            return self._error(ProviderUnavailable, f"握手失败 HTTP {status}")
        return self._error(ProviderError, f"握手被拒绝 HTTP {status}")

    def _task_error(self, header: dict[str, Any]) -> ProviderError:
        """task-failed 按 error_code / error_message 关键词归类（官方未公开完整错误码表）。"""
        code = str(header.get("error_code") or "")
        message = str(header.get("error_message") or "")
        text = f"{code} {message}".lower()
        kind: type[ProviderError] = ProviderError
        if any(k in text for k in ("throttl", "ratequota", "rate limit", "too many")):
            kind = RateLimited
        elif any(k in text for k in ("auth", "apikey", "api key", "accessdenied", "arrearage")):
            kind = AuthError
        elif "timeout" in text:
            kind = ProviderTimeout
        elif any(k in text for k in ("internal", "server error", "unavailable")):
            kind = ProviderUnavailable
        log.warning("asr_task_failed", error_code=code, error_message=message)
        return self._error(kind, f"任务失败 {code}: {message}")
