"""百炼 Qwen-TTS Realtime 流式合成（qwen3-tts-flash-realtime，Bodega、Ryan 等 Qwen3-TTS 音色）。

协议（https://help.aliyun.com/zh/model-studio/qwen-tts-realtime ）：
连接后服务端先发 session.created；
客户端 session.update 设置音色、格式等（等 session.updated）；input_text_buffer.append 追加文本、
input_text_buffer.commit 提交合成；服务端每次提交生成一个 response：
response.audio.delta（base64 音频）… response.done（usage.characters）。出错时发 error 事件。

- 用 commit 模式（官方称延迟最低）：客户端按句末标点切分，每凑满一句就 append + commit，
  整句合成、韵律完整；文本结束时把剩余部分提交。一次 synthesize() 的所有提交都完成即结束。
- 会话不结束（不发 session.finish，它会让服务端关闭连接），连接放回空闲槽给下一句复用；
  音色、采样率、语种变化时才重新发 session.update。warmup() 建连并按默认参数完成 session.update。
- 取消（打断）：协议没有取消当前合成的事件，直接关闭连接（不复用）；
  打断后可调用 warmup() 预建新连接。
- 首包耗时：发出第一段文本到收到第一块音频。
- 计费字符：2026-10-17 实测，response.done 的 usage.characters 是整个会话（连接）的累计值，
  计数规则与 CosyVoice 相同（汉字计 2、其他计 1）。所以按连接记住上一次的累计值，
  本次用量 = 本次各 response 的增量之和（累计值变小视为会话重新计数）。没有正常结束时
  与按已发送文本的估计值（bailian_tts.estimate_characters）取较大值。
- 建连、空闲槽、断线与握手错误映射：见 providers/dashscope_ws.py。日志不记录文本。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import uuid
import weakref
from collections.abc import AsyncGenerator, AsyncIterable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import structlog
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from interview.providers.bailian_tts import estimate_characters
from interview.providers.base import (
    BadResponse,
    GeneratorStream,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    TTSStream,
    TTSUsage,
)
from interview.providers.dashscope_ws import DashScopeTaskClient

# 这些字符之后可以断句提交；英文句点后面要跟空白才算（避免拆开小数、缩写）
_SENTENCE_END = "。！？!?；;\n"
LANGUAGE_TYPES: Mapping[str, str] = {
    "zh": "Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "ru": "Russian",
}

log = structlog.get_logger(__name__)


def split_sentence(buffer: str) -> tuple[str, str]:
    """返回（可以提交的完整句子部分, 剩余部分）；没有句末标点时前者为空。"""
    cut = -1
    for index, char in enumerate(buffer):
        english_stop = char == "." and index + 1 < len(buffer) and buffer[index + 1].isspace()
        if char in _SENTENCE_END or english_stop:
            cut = index
    return buffer[: cut + 1], buffer[cut + 1 :]


def realtime_url(base_url: str, model: str) -> str:
    """配置的地址不带 model 参数时补上（协议用查询参数指定型号）。"""
    if "model=" in base_url:
        return base_url
    return f"{base_url}{'&' if '?' in base_url else '?'}model={model}"


@dataclass
class _State:
    sent: list[str] = field(default_factory=list[str])
    commits: int = 0
    done: int = 0
    reported: int = 0  # 本次合成的计费字符（累计值的增量之和）
    responses: list[int] = field(default_factory=list[int])  # 各 response 返回的会话累计值
    events: list[str] = field(default_factory=list[str])  # 收到的事件类型（排查卡住用）

    def characters(self, *, finished: bool) -> int:
        if finished and self.responses:
            return self.reported
        return max(self.reported, estimate_characters("".join(self.sent)))


class BailianTTSRealtime:
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
        self._sessions: weakref.WeakKeyDictionary[ClientConnection, dict[str, Any]] = (
            weakref.WeakKeyDictionary()
        )
        # 每个连接上一次收到的 usage.characters（会话累计值）
        self._billed: weakref.WeakKeyDictionary[ClientConnection, int] = weakref.WeakKeyDictionary()
        self._ws = DashScopeTaskClient(
            name="tts_realtime",
            provider=provider,
            model=model,
            api_key=api_key,
            url=realtime_url(url, model),
            timeout_s=timeout_s,
            on_connect=self._await_created,
        )

    def synthesize(
        self,
        text: AsyncIterable[str],
        *,
        voice: str | None = None,
        language: str | None = None,
        sample_rate: int = 24000,
    ) -> TTSStream:
        session = self._session(voice, language, sample_rate)
        return GeneratorStream(
            lambda stream: self._ws.guarded(self._run(stream, text, session, _State()))
        )

    async def warmup(self) -> None:
        """建连并按默认音色、24 kHz 完成 session.update，首句不必再等。"""
        conn = await self._ws.guard_call(self._ready(self._session(None, None, 24000)))
        await self._ws.release(conn)

    async def aclose(self) -> None:
        await self._ws.aclose()

    # ---- 会话 ----

    def _session(self, voice: str | None, language: str | None, sample_rate: int) -> dict[str, Any]:
        chosen = voice or self._voice
        if not chosen:
            raise ValueError("未指定音色：调用时传 voice 或配置 TTS_VOICE")
        session: dict[str, Any] = {"mode": "commit", "response_format": "pcm"}
        session.update(self._extra_params)
        session.update(
            voice=chosen,
            sample_rate=sample_rate,
            language_type=LANGUAGE_TYPES.get(language or "", "Auto"),
        )
        return session

    async def _await_created(self, conn: ClientConnection) -> None:
        async with asyncio.timeout(self._ws.timeout_s):
            while True:
                event = self._parse(await conn.recv())
                if event.get("type") == "session.created":
                    return
                if event.get("type") == "error":
                    raise self._failure(event)

    async def _ready(self, session: dict[str, Any]) -> ClientConnection:
        """取连接并确保会话参数一致；复用的连接已断开时换新连接重试一次（还没发文本，安全）。"""
        for attempt in (1, 2):
            conn: ClientConnection | None = None
            try:
                conn = await self._ws.acquire()
                if self._sessions.get(conn) != session:
                    await self._send(conn, {"type": "session.update", "session": session})
                    async with asyncio.timeout(self._ws.timeout_s):
                        while True:
                            event = self._parse(await conn.recv())
                            if event.get("type") == "session.updated":
                                break
                            if event.get("type") == "error":
                                raise self._failure(event)
                    self._sessions[conn] = session
                return conn
            except ProviderError:
                await self._ws.discard(conn)
                raise
            except TimeoutError as exc:
                await self._ws.discard(conn)
                raise self._ws.error(ProviderTimeout, "建连或等待 session.updated 超时") from exc
            except (ConnectionClosed, OSError) as exc:
                await self._ws.discard(conn)
                if attempt == 2:
                    raise self._ws.error(ProviderUnavailable, "合成开始前连接失败") from exc
                log.warning("tts_realtime_reconnect", error=type(exc).__name__)
            except BaseException:
                await self._ws.discard(conn)
                raise
        raise AssertionError("unreachable")

    # ---- 一次合成 ----

    async def _run(
        self,
        stream: GeneratorStream[bytes, TTSUsage],
        text: AsyncIterable[str],
        session: dict[str, Any],
        state: _State,
    ) -> AsyncGenerator[bytes]:
        conn = await self._ready(session)
        previous = self._billed.get(conn, 0)

        async def send_text() -> None:
            pending = ""
            async for chunk in text:
                if not chunk:
                    continue
                stream.mark_start()
                pending += chunk
                ready, pending = split_sentence(pending)
                if ready:
                    await self._commit(conn, ready, state)
            if pending:
                await self._commit(conn, pending, state)

        sender = asyncio.create_task(send_text())
        finished = False
        try:
            while not (sender.done() and state.done >= state.commits):
                try:
                    raw = await self._next(conn, sender)
                except ProviderTimeout:
                    log.warning(
                        "tts_realtime_stalled",
                        commits=state.commits,
                        done=state.done,
                        events=state.events[-8:],
                    )
                    raise
                if raw is None:
                    continue
                event = self._parse(raw)
                kind = event.get("type")
                if kind != "response.audio.delta" or state.events[-1:] != [kind]:
                    state.events.append(str(kind))
                if kind == "response.audio.delta":
                    audio = self._decode(event.get("delta"))
                    if audio:
                        stream.mark_first()
                        yield audio
                elif kind == "response.done":
                    response: dict[str, Any] = event.get("response") or {}
                    usage: dict[str, Any] = response.get("usage") or {}
                    cumulative = int(usage.get("characters") or 0)
                    # 累计值变小说明服务端重新计数，本次增量就是新的累计值
                    state.reported += (
                        cumulative - previous if cumulative >= previous else cumulative
                    )
                    previous = cumulative
                    self._billed[conn] = cumulative
                    state.responses.append(cumulative)
                    state.done += 1
                elif kind == "error":
                    raise self._failure(event)
            sender.result()
            finished = True
        finally:
            if not sender.done():
                sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            stream.usage = TTSUsage(characters=state.characters(finished=finished))
            log.info(
                "tts_realtime_end",
                completed=finished,
                characters=stream.usage.characters,
                responses=state.responses,
                estimated=estimate_characters("".join(state.sent)),
                commits=state.commits,
                first_latency_ms=stream.first_latency_ms,
            )
            if finished:
                await self._ws.release(conn)
            else:
                # 协议没有取消当前合成的事件：关闭连接，不复用
                self._sessions.pop(conn, None)
                self._billed.pop(conn, None)
                await self._ws.discard(conn)

    async def _commit(self, conn: ClientConnection, sentence: str, state: _State) -> None:
        await self._send(conn, {"type": "input_text_buffer.append", "text": sentence})
        await self._send(conn, {"type": "input_text_buffer.commit"})
        state.sent.append(sentence)
        state.commits += 1

    async def _next(self, conn: ClientConnection, sender: asyncio.Task[None]) -> str | bytes | None:
        """等下一条服务端消息；发送端刚结束时返回 None（让调用方检查是否已全部完成）。

        发送端出错时立即抛出；发送端结束后每条消息都有超时。
        """
        recv = asyncio.ensure_future(conn.recv())
        try:
            waiting: set[asyncio.Future[Any]] = {recv} if sender.done() else {recv, sender}
            timeout = self._ws.timeout_s if sender.done() else None
            done, _ = await asyncio.wait(
                waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if recv in done:
                return recv.result()
            if not done:
                raise self._ws.error(ProviderTimeout, "文本发完后等待音频超时")
            sender.result()
            return None
        finally:
            if not recv.done():
                recv.cancel()

    async def _send(self, conn: ClientConnection, event: dict[str, Any]) -> None:
        payload = {"event_id": uuid.uuid4().hex, **event}
        await self._ws.send(conn, json.dumps(payload, ensure_ascii=False))

    # ---- 解析 ----

    def _parse(self, data: str | bytes) -> dict[str, Any]:
        try:
            event = json.loads(data)
        except ValueError as exc:
            raise self._ws.error(BadResponse, "服务端消息不是 JSON") from exc
        if not isinstance(event, dict):
            raise self._ws.error(BadResponse, "服务端消息格式不正确")
        return cast(dict[str, Any], event)

    def _decode(self, delta: Any) -> bytes:
        if not isinstance(delta, str):
            raise self._ws.error(BadResponse, "音频数据缺失")
        try:
            return base64.b64decode(delta, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise self._ws.error(BadResponse, "音频不是合法的 base64") from exc

    def _failure(self, event: dict[str, Any]) -> ProviderError:
        error: dict[str, Any] = event.get("error") or {}
        return self._ws.failure(str(error.get("code") or ""), str(error.get("message") or ""))
