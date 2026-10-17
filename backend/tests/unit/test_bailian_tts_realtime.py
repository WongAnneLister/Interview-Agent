"""Qwen-TTS Realtime 合成：用本地假 WebSocket 服务端验证。

会话配置、按句提交、音频解码、复用、取消与错误映射。
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import SecretStr
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from interview.config import BailianSettings, TTSSettings
from interview.providers.bailian_tts_realtime import (
    BailianTTSRealtime,
    realtime_url,
    split_sentence,
)
from interview.providers.base import BadResponse, ProviderTimeout, RateLimited
from interview.providers.registry import default_factories


@dataclass
class Behavior:
    close_on_update: int = 0  # 前 n 个连接收到 session.update 就断开
    fail_on_append: tuple[str, str] | None = None
    frames: int = 2
    frame_delay_s: float = 0.0
    no_done: bool = False
    bad_audio: bool = False


@dataclass
class FakeServer:
    behavior: Behavior
    connections: int = 0
    received: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    sent_audio: bytes = b""
    server: Server | None = None

    @property
    def url(self) -> str:
        assert self.server is not None
        port = next(iter(self.server.sockets)).getsockname()[1]
        return f"ws://127.0.0.1:{port}/api-ws/v1/realtime"

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.received if m["type"] == kind]

    async def handler(self, ws: ServerConnection) -> None:
        self.connections += 1
        index = self.connections
        b = self.behavior
        buffer = ""
        total = 0  # 与真实服务一致：usage.characters 是整个会话的累计值
        try:
            await ws.send(json.dumps({"type": "session.created", "session": {"id": "s"}}))
            async for message in ws:
                event = json.loads(message)
                self.received.append(event)
                kind = event["type"]
                if kind == "session.update":
                    if index <= b.close_on_update:
                        await ws.close()
                        return
                    await ws.send(
                        json.dumps({"type": "session.updated", "session": event["session"]})
                    )
                elif kind == "input_text_buffer.append":
                    if b.fail_on_append is not None:
                        code, message_text = b.fail_on_append
                        error = {"code": code, "message": message_text}
                        await ws.send(json.dumps({"type": "error", "error": error}))
                        continue
                    buffer += event["text"]
                elif kind == "input_text_buffer.commit":
                    text, buffer = buffer, ""
                    await ws.send(json.dumps({"type": "input_text_buffer.committed"}))
                    await ws.send(json.dumps({"type": "response.created"}))
                    for i in range(b.frames):
                        frame = f"{text}#{i};".encode()
                        self.sent_audio += frame
                        delta = "%%%" if b.bad_audio else base64.b64encode(frame).decode()
                        await ws.send(json.dumps({"type": "response.audio.delta", "delta": delta}))
                        await asyncio.sleep(b.frame_delay_s)
                    total += len(text)
                    if not b.no_done:
                        response = {"status": "completed", "usage": {"characters": total}}
                        await ws.send(json.dumps({"type": "response.done", "response": response}))
        except ConnectionClosed:
            pass


@pytest.fixture
async def make_server() -> AsyncIterator[Any]:
    servers: list[Server] = []

    async def start(behavior: Behavior | None = None) -> FakeServer:
        fake = FakeServer(behavior or Behavior())
        fake.server = await serve(fake.handler, "127.0.0.1", 0)
        servers.append(fake.server)
        return fake

    yield start
    for server in servers:
        server.close()
        await server.wait_closed()


def client(server: FakeServer, *, timeout_s: float = 2.0, **extra: Any) -> BailianTTSRealtime:
    return BailianTTSRealtime(
        model="qwen3-tts-flash-realtime",
        api_key="sk-test",
        url=server.url,
        timeout_s=timeout_s,
        voice="Ryan",
        extra_params=extra,
    )


async def texts(*chunks: str, hang: asyncio.Event | None = None) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk
    if hang is not None:
        await hang.wait()


async def synth(tts: BailianTTSRealtime, *chunks: str, **kwargs: Any) -> tuple[bytes, Any]:
    async with tts.synthesize(texts(*chunks), **kwargs) as stream:
        audio = b"".join([frame async for frame in stream])
    return audio, stream


def test_split_sentence() -> None:
    assert split_sentence("你好。请介绍") == ("你好。", "请介绍")
    assert split_sentence("Hi. How are") == ("Hi.", " How are")
    assert split_sentence("version 3.5 is") == ("", "version 3.5 is")
    assert split_sentence("真的吗？好！还有") == ("真的吗？好！", "还有")
    assert split_sentence("没有句号") == ("", "没有句号")


def test_realtime_url_adds_model() -> None:
    base = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    assert realtime_url(base, "m") == f"{base}?model=m"
    assert realtime_url(f"{base}?model=x", "m") == f"{base}?model=x"
    assert realtime_url(f"{base}?a=1", "m") == f"{base}?a=1&model=m"


async def test_session_config_sentence_commits_and_audio(make_server: Any) -> None:
    server = await make_server()
    tts = client(server, speech_rate=1.1)
    chunks = ["你好，我是", "面试官。请", "介绍一下", "自己。", "Then"]
    audio, stream = await synth(tts, *chunks, language="en", sample_rate=16000)
    await tts.aclose()

    (update,) = server.of("session.update")
    assert update["session"] == {
        "mode": "commit",
        "response_format": "pcm",
        "speech_rate": 1.1,
        "voice": "Ryan",
        "sample_rate": 16000,
        "language_type": "English",
    }
    assert [e["text"] for e in server.of("input_text_buffer.append")] == [
        "你好，我是面试官。",
        "请介绍一下自己。",
        "Then",
    ]
    assert len(server.of("input_text_buffer.commit")) == 3
    assert all(e.get("event_id") for e in server.received)
    assert audio == server.sent_audio
    assert stream.usage.characters == 9 + 8 + 4  # 会话累计值 9、17、21 的增量之和
    assert stream.first_latency_ms is not None


async def test_connection_and_session_are_reused(make_server: Any) -> None:
    server = await make_server()
    tts = client(server)
    await tts.warmup()  # 默认音色、24 kHz
    _, first = await synth(tts, "第一句。")
    _, second = await synth(tts, "第二句话。")
    # 同一会话里累计值继续增长（4 → 9），第二句只计自己的增量
    assert (first.usage.characters, second.usage.characters) == (4, 5)
    await synth(tts, "换个音色。", voice="Bodega")
    await tts.aclose()
    assert server.connections == 1
    voices = [e["session"]["voice"] for e in server.of("session.update")]
    assert voices == ["Ryan", "Bodega"]  # 参数没变时不重复 session.update


async def test_cancel_closes_connection(make_server: Any) -> None:
    server = await make_server(Behavior(frames=20, frame_delay_s=0.02))
    tts = client(server)
    stream = tts.synthesize(texts("一段很长的回答。", hang=asyncio.Event()))
    await anext(stream)
    await stream.aclose()
    assert stream.usage is not None
    assert (
        stream.usage.characters == 15
    )  # 没有 response.done：按已发送文本估计（7 个汉字 × 2 + 句号）
    await synth(tts, "下一句。")
    await tts.aclose()
    assert server.connections == 2


async def test_reconnects_once_before_sending_text(make_server: Any) -> None:
    server = await make_server(Behavior(close_on_update=1))
    tts = client(server)
    audio, _ = await synth(tts, "你好。")
    await tts.aclose()
    assert audio
    assert server.connections == 2


async def test_error_event_is_mapped(make_server: Any) -> None:
    server = await make_server(Behavior(fail_on_append=("Throttling", "too many requests")))
    with pytest.raises(RateLimited):
        await synth(client(server), "你好。")


async def test_timeout_waiting_for_response_done(make_server: Any) -> None:
    server = await make_server(Behavior(no_done=True))
    with pytest.raises(ProviderTimeout):
        await synth(client(server, timeout_s=0.2), "你好。")


async def test_bad_audio_is_bad_response(make_server: Any) -> None:
    server = await make_server(Behavior(bad_audio=True))
    with pytest.raises(BadResponse):
        await synth(client(server), "你好。")


def test_missing_voice_raises(make_server: Any) -> None:
    tts = BailianTTSRealtime(model="m", api_key="k", url="ws://x", timeout_s=1)
    with pytest.raises(ValueError, match="音色"):
        tts.synthesize(texts("hi"))


def test_registry_builds_realtime_adapter() -> None:
    vendor = BailianSettings(api_key=SecretStr("sk"))  # pyright: ignore[reportCallIssue]
    settings: Any = type("S", (), {"bailian": vendor, "deepseek": vendor})()
    role = TTSSettings(  # pyright: ignore[reportCallIssue]
        provider="bailian_realtime",
        model="qwen3-tts-flash-realtime",
        base_url="wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
        voice="Ryan",
    )
    tts = default_factories(settings).tts["bailian_realtime"](role)
    assert isinstance(tts, BailianTTSRealtime)
