"""百炼流式合成：用本地假 WebSocket 服务端验证协议顺序、音频输出、用量、取消与复用、错误映射。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from interview.config import BailianSettings, TTSSettings
from interview.providers.bailian_tts import BailianTTS, estimate_characters
from interview.providers.base import AuthError, ProviderTimeout, RateLimited
from interview.providers.registry import ProviderNotConfigured, default_factories


@dataclass
class Behavior:
    reject_status: int | None = None
    close_on_run_task: int = 0  # 前 n 个连接收到 run-task 就断开
    fail_with: tuple[str, str] | None = None
    frames_per_chunk: int = 2
    frame_delay_s: float = 0.0
    no_finish: bool = False
    ack_cancel: bool = True


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
        return f"ws://127.0.0.1:{port}"

    def actions(self) -> list[str]:
        return [m["header"]["action"] for m in self.received]

    def process_request(self, conn: ServerConnection, request: Request) -> Response | None:
        if self.behavior.reject_status is not None:
            return conn.respond(HTTPStatus(self.behavior.reject_status), "rejected\n")
        return None

    async def audio(self, ws: ServerConnection, text: str) -> None:
        for index in range(self.behavior.frames_per_chunk):
            await ws.send(event("result-generated", output={"type": "sentence-synthesis"}))
            frame = f"{text}#{index};".encode()
            self.sent_audio += frame
            await ws.send(frame)
            await asyncio.sleep(self.behavior.frame_delay_s)

    async def handler(self, ws: ServerConnection) -> None:
        self.connections += 1
        index = self.connections
        b = self.behavior
        characters = 0
        try:
            async for message in ws:
                command = json.loads(message)
                self.received.append(command)
                action = command["header"]["action"]
                if action == "run-task":
                    if index <= b.close_on_run_task:
                        await ws.close()
                        return
                    if b.fail_with is not None:
                        await ws.send(event("task-failed", code=b.fail_with))
                        await ws.close()
                        return
                    characters = 0
                    await ws.send(event("task-started"))
                elif action == "continue-task":
                    text = command["payload"]["input"]["text"]
                    characters += len(text)  # 假服务端按 1 字 1 字符计
                    await self.audio(ws, text)
                elif command["payload"]["input"].get("directive") == "cancel":
                    if not b.ack_cancel:
                        continue
                    await ws.send(b"stale-audio")
                    await ws.send(event("task-finished", usage={"characters": characters}))
                else:
                    if b.no_finish:
                        continue
                    end = {"type": "sentence-end"}
                    await ws.send(event("result-generated", output=end, usage={"characters": 1}))
                    await ws.send(event("task-finished", usage={"characters": characters}))
        except ConnectionClosed:
            pass


def event(
    name: str,
    *,
    output: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    code: tuple[str, str] | None = None,
) -> str:
    header: dict[str, Any] = {"task_id": "t", "event": name, "attributes": {}}
    if code is not None:
        header.update(error_code=code[0], error_message=code[1])
    return json.dumps({"header": header, "payload": {"output": output or {}, "usage": usage}})


@pytest.fixture
async def make_server() -> AsyncIterator[Any]:
    servers: list[Server] = []

    async def start(behavior: Behavior | None = None) -> FakeServer:
        fake = FakeServer(behavior or Behavior())
        fake.server = await serve(
            fake.handler, "127.0.0.1", 0, process_request=fake.process_request
        )
        servers.append(fake.server)
        return fake

    yield start
    for server in servers:
        server.close()
        await server.wait_closed()


def tts(server: FakeServer, *, timeout_s: float = 2.0, **extra: Any) -> BailianTTS:
    return BailianTTS(
        model="cosyvoice-v3-flash",
        api_key="sk-test",
        url=server.url,
        timeout_s=timeout_s,
        voice="longanlang_v3",
        extra_params=extra,
    )


async def texts(*chunks: str, hang: asyncio.Event | None = None) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk
    if hang is not None:
        await hang.wait()  # 模拟大模型还没说完


async def synth(client: BailianTTS, *chunks: str, **kwargs: Any) -> tuple[bytes, Any]:
    async with client.synthesize(texts(*chunks), **kwargs) as stream:
        audio = b"".join([frame async for frame in stream])
    return audio, stream


# ---- 协议与输出 ----


async def test_protocol_order_audio_and_usage(make_server: Any) -> None:
    server = await make_server()
    client = tts(server, rate=1.1)
    audio, stream = await synth(client, "你好，", "", "world", language="zh", sample_rate=16000)
    await client.aclose()

    assert audio == server.sent_audio
    assert stream.usage.characters == 8  # 取服务端 task-finished 的累计值
    assert stream.first_latency_ms is not None
    assert server.actions() == ["run-task", "continue-task", "continue-task", "finish-task"]
    run, first, second, finish = server.received
    payload = run["payload"]
    assert (payload["task_group"], payload["task"], payload["function"]) == (
        "audio",
        "tts",
        "SpeechSynthesizer",
    )
    assert payload["model"] == "cosyvoice-v3-flash"
    assert payload["parameters"] == {
        "text_type": "PlainText",
        "format": "pcm",
        "rate": 1.1,
        "voice": "longanlang_v3",
        "sample_rate": 16000,
        "language_hints": ["zh"],
    }
    assert [first["payload"]["input"]["text"], second["payload"]["input"]["text"]] == [
        "你好，",
        "world",
    ]
    task_ids = {m["header"]["task_id"] for m in server.received}
    assert len(task_ids) == 1
    assert all(m["header"]["streaming"] == "duplex" for m in server.received)
    assert finish["payload"]["input"] == {}


async def test_voice_override_and_missing_voice(make_server: Any) -> None:
    server = await make_server()
    client = tts(server)
    await synth(client, "hi", voice="longshuo_v3")
    await client.aclose()
    assert server.received[0]["payload"]["parameters"]["voice"] == "longshuo_v3"
    bare = BailianTTS(model="m", api_key="k", url=server.url, timeout_s=1)
    with pytest.raises(ValueError, match="音色"):
        bare.synthesize(texts("hi"))


def test_estimate_characters_counts_han_twice() -> None:
    assert estimate_characters("Hi，你好。") == 2 + 1 + 2 * 2 + 1
    # live 实测的句子：37 个汉字 + 4 个全角标点 = 78
    zh = "你好，我是今天的面试官。先请你用两分钟介绍一下最近做的项目，重点说说你负责的部分。"
    assert estimate_characters(zh) == 78


# ---- 复用、预热、取消 ----


async def test_warmup_and_reuse_share_one_connection(make_server: Any) -> None:
    server = await make_server()
    client = tts(server)
    await client.warmup()
    await synth(client, "第一句。")
    await synth(client, "第二句。")
    await client.aclose()
    assert server.connections == 1


async def test_cancel_sends_directive_and_keeps_connection(make_server: Any) -> None:
    server = await make_server(Behavior(frames_per_chunk=5, frame_delay_s=0.02))
    client = tts(server)
    hang = asyncio.Event()
    stream = client.synthesize(texts("一段很长的回答", hang=hang))
    with capture_logs() as logs:
        first = await anext(stream)
        await stream.aclose()  # 候选人打断
    assert first.startswith("一段很长的回答#0".encode())
    assert server.actions()[-1] == "finish-task"
    assert server.received[-1]["payload"]["input"] == {"directive": "cancel"}
    # 没有正常结束：服务端报 7，估计值 14（7 个汉字），取较大值
    assert stream.usage is not None
    assert stream.usage.characters == 14
    (end,) = [e for e in logs if e["event"] == "tts_task_end"]
    assert (end["cancelled"], end["completed"], end["reported"]) == (True, False, 7)

    audio, _ = await synth(client, "继续。")
    await client.aclose()
    # 取消时剩余的音频已被丢弃，下一句只有自己的音频
    assert audio == b"".join(f"继续。#{i};".encode() for i in range(5))
    assert server.connections == 1  # 取消后连接复用


async def test_cancel_without_ack_discards_connection(make_server: Any) -> None:
    server = await make_server(Behavior(ack_cancel=False))
    client = tts(server, timeout_s=0.3)
    stream = client.synthesize(texts("打断", hang=asyncio.Event()))
    await anext(stream)
    await stream.aclose()
    await synth(client, "下一句。")
    await client.aclose()
    assert server.connections == 2


async def test_task_cancellation_also_cancels_on_server(make_server: Any) -> None:
    server = await make_server(Behavior(frames_per_chunk=50, frame_delay_s=0.02))
    client = tts(server)
    stream = client.synthesize(texts("打断我", hang=asyncio.Event()))

    async def consume() -> None:
        async for _ in stream:
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.aclose()
    assert {"directive": "cancel"} in [m["payload"]["input"] for m in server.received]


async def test_reconnects_once_before_task_started(make_server: Any) -> None:
    server = await make_server(Behavior(close_on_run_task=1))
    client = tts(server)
    audio, _ = await synth(client, "你好。")
    await client.aclose()
    assert audio
    assert server.connections == 2


# ---- 错误与超时 ----


async def test_task_failed_and_handshake_errors(make_server: Any) -> None:
    server = await make_server(Behavior(fail_with=("Throttling.RateQuota", "too many")))
    with pytest.raises(RateLimited):
        await synth(tts(server), "你好")
    server = await make_server(Behavior(reject_status=401))
    with pytest.raises(AuthError):
        await synth(tts(server), "你好")


async def test_timeout_waiting_for_task_finished(make_server: Any) -> None:
    server = await make_server(Behavior(no_finish=True))
    with pytest.raises(ProviderTimeout):
        await synth(tts(server, timeout_s=0.2), "你好")


# ---- 注册表 ----


def test_factory_requires_voice_and_realtime_is_reserved() -> None:
    vendor = BailianSettings(api_key=SecretStr("sk"))  # pyright: ignore[reportCallIssue]

    def role(provider: str, voice: str) -> Any:
        return TTSSettings(  # pyright: ignore[reportCallIssue]
            provider=provider,
            model="cosyvoice-v3-flash",
            base_url="wss://tts.example.com/api-ws/v1/inference",
            voice=voice,
        )

    settings: Any = type("S", (), {"bailian": vendor, "deepseek": vendor})()
    factories = default_factories(settings).tts
    assert isinstance(factories["bailian"](role("bailian", "longanlang_v3")), BailianTTS)
    with pytest.raises(ProviderNotConfigured, match="TTS_VOICE"):
        factories["bailian"](role("bailian", ""))
    with pytest.raises(ProviderNotConfigured, match="尚未实现"):
        factories["bailian_realtime"](role("bailian_realtime", "x"))
