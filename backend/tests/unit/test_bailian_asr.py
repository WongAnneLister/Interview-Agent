"""百炼流式识别：用本地假 WebSocket 服务端验证协议顺序、事件解析、复用与重连、错误映射、超时。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from interview.config import BailianSettings, STTSettings
from interview.providers.bailian_asr import BailianASR
from interview.providers.base import (
    AuthError,
    BadResponse,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
    STTEvent,
)
from interview.providers.registry import ProviderNotConfigured, bailian_asr_factory

FRAME = b"\x01\x00" * 1600  # 100 ms @ 16 kHz


@dataclass
class Behavior:
    """假服务端的行为开关。"""

    reject_status: int | None = None  # 握手直接拒绝
    close_on_run_task: int = 0  # 前 n 个连接收到 run-task 就断开
    fail_with: tuple[str, str] | None = None  # 收到 run-task 回 task-failed
    no_start: bool = False
    close_on_audio: bool = False
    no_finish: bool = False
    empty_final: bool = False  # 结束时多发一个空文本的最终结果（噪声）
    bad_json: bool = False
    duration: float = 1.5


@dataclass
class FakeServer:
    behavior: Behavior
    connections: int = 0
    auth: list[str] = field(default_factory=list[str])
    received: list[Any] = field(default_factory=list[Any])  # 文本指令为 dict，音频为字节数
    server: Server | None = None

    @property
    def url(self) -> str:
        assert self.server is not None
        port = next(iter(self.server.sockets)).getsockname()[1]
        return f"ws://127.0.0.1:{port}"

    def commands(self, action: str) -> list[dict[str, Any]]:
        return [m for m in self.received if isinstance(m, dict) and m["header"]["action"] == action]

    def process_request(self, conn: ServerConnection, request: Request) -> Response | None:
        self.auth.append(request.headers.get("Authorization", ""))
        if self.behavior.reject_status is not None:
            return conn.respond(HTTPStatus(self.behavior.reject_status), "rejected\n")
        return None

    async def handler(self, ws: ServerConnection) -> None:
        self.connections += 1
        index = self.connections
        b = self.behavior
        with_task: str | None = None
        frames = 0
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    self.received.append(len(message))
                    frames += 1
                    if b.close_on_audio:
                        await ws.close()
                        return
                    if b.bad_json:
                        await ws.send("{oops")
                        continue
                    await ws.send(result(with_task, f"partial{frames}", end=False, begin=0))
                    continue
                command = json.loads(message)
                self.received.append(command)
                action = command["header"]["action"]
                task_id = command["header"]["task_id"]
                if action == "run-task":
                    if index <= b.close_on_run_task:
                        await ws.close()
                        return
                    if b.fail_with is not None:
                        await ws.send(event(task_id, "task-failed", *b.fail_with))
                        await ws.close()
                        return
                    if b.no_start:
                        continue
                    with_task, frames = task_id, 0
                    await ws.send(event(task_id, "task-started"))
                elif action == "finish-task":
                    if b.no_finish:
                        continue
                    await ws.send(heartbeat(task_id))
                    if b.empty_final:
                        await ws.send(result(task_id, "  ", end=True, duration=b.duration))
                    await ws.send(result(task_id, "hello 世界", end=True, duration=b.duration))
                    await ws.send(event(task_id, "task-finished"))
        except ConnectionClosed:
            pass


def event(task_id: str | None, name: str, code: str = "", message: str = "") -> str:
    header: dict[str, Any] = {"task_id": task_id, "event": name, "attributes": {}}
    if code:
        header.update(error_code=code, error_message=message)
    return json.dumps({"header": header, "payload": {}})


def result(
    task_id: str | None, text: str, *, end: bool, begin: int = 0, duration: float | None = None
) -> str:
    sentence: dict[str, Any] = {
        "begin_time": begin,
        "end_time": 900 if end else None,
        "text": text,
        "sentence_end": end,
        "words": [{"begin_time": 0, "end_time": 320, "text": text, "punctuation": ""}],
    }
    if end:
        sentence["language"] = "zh"
    usage = {"duration": duration} if duration is not None else None
    return json.dumps(
        {
            "header": {"task_id": task_id, "event": "result-generated", "attributes": {}},
            "payload": {"output": {"sentence": sentence}, "usage": usage},
        }
    )


def heartbeat(task_id: str) -> str:
    sentence = {"begin_time": 0, "end_time": 0, "text": "", "heartbeat": True, "sentence_id": 0}
    return json.dumps(
        {
            "header": {"task_id": task_id, "event": "result-generated", "attributes": {}},
            "payload": {"output": {"sentence": sentence}, "usage": None},
        }
    )


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


def asr(server: FakeServer, *, timeout_s: float = 2.0, **extra: Any) -> BailianASR:
    return BailianASR(
        model="fun-asr-realtime",
        api_key="sk-test",
        url=server.url,
        timeout_s=timeout_s,
        extra_params=extra,
    )


async def frames(count: int, *, pause_s: float = 0.0) -> AsyncIterator[bytes]:
    for _ in range(count):
        yield FRAME
        if pause_s:
            await asyncio.sleep(pause_s)


async def collect(client: BailianASR, count: int = 2, **kwargs: Any) -> tuple[list[STTEvent], Any]:
    async with client.stream(frames(count), **kwargs) as stream:
        events = [e async for e in stream]
    return events, stream


# ---- 协议与事件 ----


async def test_protocol_order_events_and_usage(make_server: Any) -> None:
    server = await make_server()
    client = asr(server, max_sentence_silence=800, language_hints=["en"])
    events, stream = await collect(client, language_hint="zh", hotword_list_id="vocab-x")
    await client.aclose()

    assert [(e.kind, e.text) for e in events] == [
        ("partial", "partial1"),
        ("partial", "partial2"),
        ("final", "hello 世界"),
    ]
    assert (events[0].start_ms, events[0].end_ms) == (0, 320)  # 中间结果用最后一个字的结束时间
    assert (events[-1].end_ms, events[-1].language) == (900, "zh")
    assert stream.usage.audio_seconds == 1.5  # 取服务端的计费时长
    assert stream.first_latency_ms is not None

    assert server.auth == ["Bearer sk-test"]
    run, audio1, audio2, finish = server.received
    assert run["header"] == {
        "action": "run-task",
        "task_id": run["header"]["task_id"],
        "streaming": "duplex",
    }
    payload = run["payload"]
    assert (payload["task_group"], payload["task"], payload["function"]) == (
        "audio",
        "asr",
        "recognition",
    )
    assert payload["model"] == "fun-asr-realtime"
    assert payload["parameters"] == {
        "format": "pcm",
        "sample_rate": 16000,
        "max_sentence_silence": 800,
        "language_hints": ["zh"],  # 调用参数覆盖配置
        "vocabulary_id": "vocab-x",
    }
    assert (audio1, audio2) == (len(FRAME), len(FRAME))
    assert finish["header"]["action"] == "finish-task"
    assert finish["header"]["task_id"] == run["header"]["task_id"]


async def test_no_language_hints_by_default(make_server: Any) -> None:
    server = await make_server()
    client = asr(server)
    await collect(client)
    await client.aclose()
    (run,) = server.commands("run-task")
    assert "language_hints" not in run["payload"]["parameters"]


async def test_billed_covers_audio_after_last_reported_duration(make_server: Any) -> None:
    # 实测：usage.duration 只报到最后一句结束，之后的音频也计费，所以取两者较大值
    server = await make_server(Behavior(duration=0.1))
    client = asr(server)
    with capture_logs() as logs:
        _, stream = await collect(client, count=3)
    await client.aclose()
    assert stream.usage.audio_seconds == pytest.approx(0.3)
    (end,) = [e for e in logs if e["event"] == "asr_task_end"]
    assert (end["sent_s"], end["completed"], end["durations"]) == (0.3, True, [0.1])
    assert "hello" not in json.dumps(logs, ensure_ascii=False, default=str)


async def test_empty_text_results_are_skipped(make_server: Any) -> None:
    server = await make_server(Behavior(empty_final=True, duration=2.0))
    client = asr(server)
    events, stream = await collect(client)
    await client.aclose()
    assert [e.text for e in events if e.kind == "final"] == ["hello 世界"]
    assert stream.usage.audio_seconds == 2.0  # 空结果的用量照常累计


# ---- 复用与重连 ----


async def test_connection_is_reused_across_tasks(make_server: Any) -> None:
    server = await make_server()
    client = asr(server)
    await collect(client)
    await collect(client)
    await client.aclose()
    assert server.connections == 1
    ids = [c["header"]["task_id"] for c in server.commands("run-task")]
    assert len(set(ids)) == 2


async def test_reconnects_once_before_task_started(make_server: Any) -> None:
    server = await make_server(Behavior(close_on_run_task=1))
    client = asr(server)
    with capture_logs() as logs:
        events, _ = await collect(client)
    await client.aclose()
    assert events[-1].kind == "final"
    assert server.connections == 2
    assert [e["event"] for e in logs].count("asr_reconnect") == 1


async def test_gives_up_after_one_reconnect(make_server: Any) -> None:
    server = await make_server(Behavior(close_on_run_task=5))
    with pytest.raises(ProviderUnavailable):
        await collect(asr(server))
    assert server.connections == 2


async def test_mid_task_drop_is_not_retried(make_server: Any) -> None:
    server = await make_server(Behavior(close_on_audio=True))
    with pytest.raises(ProviderUnavailable):
        await collect(asr(server))
    assert server.connections == 1


async def test_aclose_midway_discards_connection(make_server: Any) -> None:
    server = await make_server()
    client = asr(server)
    stream = client.stream(frames(50, pause_s=0.05))
    first = await anext(stream)
    assert first.kind == "partial"
    await stream.aclose()
    assert stream.usage is not None
    assert stream.usage.audio_seconds == pytest.approx(0.1)  # 没有最终结果，按已发送音频估计
    await collect(client)
    await client.aclose()
    assert server.connections == 2


# ---- 错误映射与超时 ----


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        ("Throttling.RateQuota", "too many requests", RateLimited),
        ("InvalidApiKey", "invalid api key", AuthError),
        ("Arrearage", "account overdue", AuthError),
        ("CLIENT_ERROR", "request timeout after 23 seconds.", ProviderTimeout),
        ("InternalError", "internal error", ProviderUnavailable),
        ("InvalidParameter", "bad sample_rate", ProviderError),
    ],
)
async def test_task_failed_mapping(
    make_server: Any, code: str, message: str, expected: type[ProviderError]
) -> None:
    server = await make_server(Behavior(fail_with=(code, message)))
    with pytest.raises(ProviderError) as info:
        await collect(asr(server))
    assert type(info.value) is expected
    assert server.connections == 1  # task-failed 不重连


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, AuthError), (403, AuthError), (429, RateLimited), (503, ProviderUnavailable)],
)
async def test_handshake_rejection_mapping(
    make_server: Any, status: int, expected: type[ProviderError]
) -> None:
    server = await make_server(Behavior(reject_status=status))
    with pytest.raises(ProviderError) as info:
        await collect(asr(server))
    assert type(info.value) is expected


async def test_timeout_waiting_for_task_started(make_server: Any) -> None:
    server = await make_server(Behavior(no_start=True))
    with pytest.raises(ProviderTimeout):
        await collect(asr(server, timeout_s=0.2))


async def test_timeout_waiting_for_task_finished(make_server: Any) -> None:
    server = await make_server(Behavior(no_finish=True))
    stream = asr(server, timeout_s=0.2).stream(frames(2))
    with pytest.raises(ProviderTimeout):
        _ = [e async for e in stream]
    assert stream.usage is not None
    assert stream.usage.audio_seconds == pytest.approx(0.2)


async def test_bad_json_is_bad_response(make_server: Any) -> None:
    server = await make_server(Behavior(bad_json=True))
    with pytest.raises(BadResponse):
        await collect(asr(server))


async def test_unreachable_server_is_unavailable() -> None:
    client = BailianASR(model="m", api_key="k", url="ws://127.0.0.1:9", timeout_s=1)
    with pytest.raises(ProviderUnavailable):
        _ = [e async for e in client.stream(frames(1))]


# ---- 注册表 ----


def stt_role(base_url: str) -> Any:
    return STTSettings(provider="bailian", model="fun-asr-realtime", base_url=base_url)  # pyright: ignore[reportCallIssue]


def test_factory_requires_key_and_ws_url() -> None:
    vendor = BailianSettings(api_key=SecretStr("sk"), base_url="https://compat")  # pyright: ignore[reportCallIssue]
    client = bailian_asr_factory(vendor)(stt_role("wss://asr.example.com/api-ws/v1/inference"))
    assert isinstance(client, BailianASR)
    with pytest.raises(ProviderNotConfigured, match="STT_BASE_URL"):
        bailian_asr_factory(vendor)(stt_role(""))


@pytest.fixture(autouse=True)
def _no_stt_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    for name in ("STT_EXTRA_BODY", "STT_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    yield


async def test_warmup_preconnects_and_is_idempotent(make_server: Any) -> None:
    server = await make_server()
    client = asr(server)
    await client.warmup()
    await client.warmup()  # 已有可用连接时不再新建
    assert server.connections == 1
    await collect(client)
    await client.aclose()
    assert server.connections == 1  # 任务直接用预热好的连接
