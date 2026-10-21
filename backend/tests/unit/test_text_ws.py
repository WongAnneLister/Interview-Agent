"""文字面试 WebSocket：TestClient + SQLite + 假厂商，跑完整一场面试，以及越权、重复连接、断线。"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlite_helpers import create_tables
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect
from test_api import make_settings
from test_orchestrator import (
    INTERVIEW_ID,
    USER,
    EvalScript,
    budget_result,
    make_plan,
    seed,
)

from interview.api.app import create_app
from interview.api.deps import routes_without_user
from interview.api.resources import Resources
from interview.config import Settings
from interview.db.models import (
    Evaluation,
    Interview,
    InterviewStatus,
    Plan,
    Resume,
    Turn,
    TurnRole,
    User,
)
from interview.db.session import make_sessionmaker
from interview.domain import report
from interview.domain.orchestrator_store import TurnRepository
from interview.domain.plan import InterviewRepository
from interview.domain.state_machine import InterviewState
from interview.observability import budget, usage
from interview.observability.budget import BudgetStatus
from interview.providers.base import LLMProvider
from interview.providers.fake import FakeLLM
from interview.providers.registry import ProviderFactories, ProviderRegistry

URL = f"/ws/interviews/{INTERVIEW_ID}/text"
CANDIDATE_TEXT = "我用了本地消息表保证可靠投递，消费端做幂等处理。"


@dataclass
class WsApi:
    client: TestClient
    sessionmaker: async_sessionmaker[AsyncSession]
    resources: Resources
    reports: list[uuid.UUID] = field(default_factory=list[uuid.UUID])

    def run[T](self, fn: Callable[[async_sessionmaker[AsyncSession]], Awaitable[T]]) -> T:
        """在应用的事件循环里执行数据库操作（aiosqlite 连接绑定在创建它的循环上）。"""
        portal = self.client.portal
        assert portal is not None
        return portal.call(fn, self.sessionmaker)

    def interview(self) -> Interview:
        async def fetch(sm: async_sessionmaker[AsyncSession]) -> Interview:
            async with sm() as session:
                row = await InterviewRepository(session, USER).get(INTERVIEW_ID)
                assert row is not None
                return row

        return self.run(fetch)

    def turns(self) -> list[Turn]:
        async def fetch(sm: async_sessionmaker[AsyncSession]) -> list[Turn]:
            async with sm() as session:
                query = TurnRepository(session, USER).select().order_by(Turn.seq)
                return list((await session.scalars(query)).all())

        return self.run(fetch)

    def set_status(self, status: InterviewStatus) -> None:
        async def update(sm: async_sessionmaker[AsyncSession]) -> None:
            async with sm() as session:
                row = await InterviewRepository(session, USER).get(INTERVIEW_ID)
                assert row is not None
                row.status = status
                await session.commit()

        self.run(update)


def build_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    user: uuid.UUID = USER,
    front_delay_s: float = 0.0,
    idle_timeout_s: float = 30.0,
) -> Iterator[WsApi]:
    settings: Settings = make_settings(user=user)
    settings.app.ws_idle_timeout_s = idle_timeout_s
    settings.app.ws_tick_interval_s = 0.05
    settings.providers.llm_front.provider = "fake"
    settings.providers.llm_front.model = "front"
    settings.providers.llm_live_eval.provider = "fake"
    settings.providers.llm_live_eval.model = "eval"
    script = EvalScript(["follow_up"])
    eval_llm = FakeLLM(reply=script.reply, model="fake-eval", chunk_size=10_000)
    front = FakeLLM(
        reply="好的。", model="fake-front", chunk_size=10_000, first_token_delay_s=front_delay_s
    )

    def factory(role: Any) -> LLMProvider:
        return eval_llm if role.model == "eval" else front

    holder: dict[str, Resources] = {}

    def resources_factory(s: Settings) -> Resources:
        # 用文件库：被取消的查询会让连接作废，内存库会随连接一起丢失
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'ws.db'}", poolclass=NullPool
        )
        registry = ProviderRegistry(s.providers, factories=ProviderFactories(llm={"fake": factory}))
        resources = Resources(
            settings=s, sessionmaker=make_sessionmaker(engine), providers=registry, engine=engine
        )
        holder["resources"] = resources
        return resources

    async def check_budget(*_args: Any, **_kwargs: Any) -> Any:
        return budget_result(BudgetStatus.OK)

    async def ignore_usage(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(budget, "check", check_budget)
    monkeypatch.setattr(usage, "record_usage", ignore_usage)
    reports: list[uuid.UUID] = []

    async def fake_report(
        interview_id: uuid.UUID, *, user_id: uuid.UUID, runner: report.ReportRunner | None
    ) -> None:
        assert runner is holder["resources"].reports
        reports.append(interview_id)

    monkeypatch.setattr(report, "request_report", fake_report)

    app = create_app(settings, resource_factory=resources_factory, init_observability=False)
    with TestClient(app) as client:
        resources = holder["resources"]
        api = WsApi(client=client, sessionmaker=resources.sessionmaker, resources=resources)
        api.reports = reports

        async def prepare(sm: async_sessionmaker[AsyncSession]) -> None:
            assert resources.engine is not None
            async with resources.engine.begin() as conn:
                await create_tables(conn, User, Resume, Interview, Plan, Turn, Evaluation)
            await seed(sm, make_plan())

        api.run(prepare)
        yield api


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[WsApi]:
    yield from build_api(monkeypatch, tmp_path)


def until(ws: WebSocketTestSession, *stops: str, idle: bool = True) -> list[dict[str, Any]]:
    """读到 ended（以及 stops 里的 type）为止；idle 时 state=listening / paused 也停。"""
    messages: list[dict[str, Any]] = []
    while True:
        message: dict[str, Any] = ws.receive_json()
        messages.append(message)
        if message["type"] in ("ended", *stops):
            return messages
        if idle and message["type"] == "state" and message["value"] in ("listening", "paused"):
            return messages


def expect_close(ws: WebSocketTestSession) -> int:
    with pytest.raises(WebSocketDisconnect) as info:
        while True:
            ws.receive_json()
    return info.value.code


def rejected(api: WsApi, url: str = URL) -> tuple[dict[str, Any], int]:
    with api.client.websocket_connect(url) as ws:
        error: dict[str, Any] = ws.receive_json()
        return error, expect_close(ws)


def wait_released(api: WsApi) -> None:
    deadline = time.monotonic() + 5
    while INTERVIEW_ID in api.resources.live_interviews:
        assert time.monotonic() < deadline, "连接没有释放"
        time.sleep(0.02)


def test_full_text_interview(api: WsApi) -> None:
    with api.client.websocket_connect(URL) as ws:
        messages = until(ws)
        assert messages[0] == {
            "type": "stage",
            "phase": "opening",
            "remaining_s": messages[0]["remaining_s"],
            "total_remaining_s": messages[0]["total_remaining_s"],
        }
        for _ in range(2):
            ws.send_json({"type": "command", "command": "skip"})
            messages += until(ws)
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        for _ in range(5):
            ws.send_json({"type": "user_text", "text": CANDIDATE_TEXT})
            messages += until(ws)
        ws.send_json({"type": "command", "command": "skip"})
        messages += until(ws, idle=False)
        assert messages[-1] == {"type": "ended"}
        # 收尾语说完就结束，前端没有发 tick
        assert expect_close(ws) == 1000

    stages = [m["phase"] for m in messages if m["type"] == "stage"]
    assert stages == [
        "opening",
        "self_intro",
        "resume_deep_dive",
        "technical",
        "candidate_questions",
        "closing",
        "ended",
    ]
    wait_released(api)
    interview = api.interview()
    assert interview.status is InterviewStatus.COMPLETED
    assert interview.ended_at is not None
    assert api.reports == [INTERVIEW_ID]
    turns = api.turns()
    assert sum(1 for t in turns if t.role is TurnRole.CANDIDATE) == 5


def test_other_users_interview_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for api in build_api(monkeypatch, tmp_path, user=uuid.uuid4()):
        error, code = rejected(api)
        assert (error["type"], error["code"], code) == ("error", "not_found", 4404)
        assert error["trace_id"]


def test_unknown_interview_is_rejected(api: WsApi) -> None:
    error, code = rejected(api, f"/ws/interviews/{uuid.uuid4()}/text")
    assert (error["code"], code) == ("not_found", 4404)


def test_second_connection_is_rejected(api: WsApi) -> None:
    with api.client.websocket_connect(URL) as first:
        until(first)
        error, code = rejected(api)
        assert (error["code"], code) == ("already_connected", 4409)
        # 第一个连接不受影响
        first.send_json({"type": "ping"})
        assert first.receive_json() == {"type": "pong"}


@pytest.mark.parametrize(
    "status", [InterviewStatus.COMPLETED, InterviewStatus.ABORTED, InterviewStatus.FAILED]
)
def test_ended_interview_is_rejected(api: WsApi, status: InterviewStatus) -> None:
    api.set_status(status)
    error, code = rejected(api)
    assert (error["code"], code) == ("interview_ended", 4410)


def test_invalid_messages_keep_connection(api: WsApi) -> None:
    with api.client.websocket_connect(URL) as ws:
        until(ws)
        for bad in ("not json", '{"type": "tick"}', '{"type": "user_text", "text": "   "}'):
            ws.send_text(bad)
            assert ws.receive_json()["code"] == "invalid_message"
        ws.send_bytes(b"{}")
        assert ws.receive_json()["code"] == "invalid_message"
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_disconnect_mid_stream_saves_state_and_allows_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for api in build_api(monkeypatch, tmp_path, front_delay_s=0.3):
        with api.client.websocket_connect(URL) as ws:
            until(ws)
            ws.send_json({"type": "command", "command": "skip"})
            assert ws.receive_json()["type"] == "stage"  # 进入自我介绍，面试官正要开口
            ws.close()
            wait_released(api)

        interview = api.interview()
        assert interview.status is InterviewStatus.IN_PROGRESS
        assert interview.state_json is not None
        saved = InterviewState.model_validate(interview.state_json["machine"])
        assert saved.phase.value == "self_intro"

        with api.client.websocket_connect(URL) as ws:
            stage = ws.receive_json()
            assert (stage["type"], stage["phase"]) == ("stage", "self_intro")
            assert ws.receive_json() == {"type": "state", "value": "listening"}


def test_idle_connection_is_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for api in build_api(monkeypatch, tmp_path, idle_timeout_s=0.3):
        with api.client.websocket_connect(URL) as ws:
            messages = until(ws, "error")
            while messages[-1]["type"] != "error":
                messages.append(ws.receive_json())
            assert messages[-1]["code"] == "idle_timeout"
            assert expect_close(ws) == 4408
        wait_released(api)
        assert api.interview().status is InterviewStatus.IN_PROGRESS


def test_websocket_route_requires_user_and_schemas_exported(api: WsApi) -> None:
    app = api.client.app
    assert routes_without_user(app) == []  # type: ignore[arg-type]
    schemas = api.client.get("/openapi.json").json()["components"]["schemas"]
    for name in ("TextClientMessage", "TextServerMessage", "UserTextMessage", "ErrorEvent"):
        assert name in schemas
    assert "type" in schemas["InterviewerDelta"]["required"]
    mapping = schemas["TextServerMessage"]["discriminator"]["mapping"]
    assert set(mapping) >= {"interviewer_delta", "ended", "pong", "error"}
