"""语音 worker：派发 metadata、AgentServer 配置、面试 agent 和会话收尾。"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from livekit.agents import llm
from pydantic import ValidationError
from test_api import make_settings

from interview.db.models import Language
from interview.voice.driver import SessionClock
from interview.voice.session import DispatchMetadata
from interview.voice.worker import (
    InterviewAgent,
    _JobState,  # pyright: ignore[reportPrivateUsage]
    build_server,
    entrypoint,
    on_session_end,
    prewarm,
)

INTERVIEW = uuid.UUID("00000000-0000-4000-8000-000000000028")
USER = uuid.UUID("00000000-0000-4000-8000-000000000001")


def test_dispatch_metadata_round_trip() -> None:
    meta = DispatchMetadata(interview_id=INTERVIEW, user_id=USER, language=Language.EN)
    assert DispatchMetadata.parse(meta.model_dump_json()) == meta


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        f'{{"interview_id": "{INTERVIEW}"}}',  # PR-26 的旧格式：缺 user_id、language
        f'{{"interview_id": "{INTERVIEW}", "user_id": "{USER}", "language": "fr"}}',
    ],
)
def test_invalid_dispatch_metadata_is_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        DispatchMetadata.parse(raw)


def test_server_is_configured_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVEKIT_AGENT_NAME_OVERRIDE", raising=False)
    settings = make_settings()
    settings.livekit.agent_name = "interview-test"
    settings.livekit.worker_idle_processes = 2
    settings.livekit.drain_timeout_s = 1800
    settings.report.total_timeout_s = 90
    server: Any = build_server(settings)
    assert server._agent_name == "interview-test"
    assert server._entrypoint_fnc is entrypoint
    assert server._setup_fnc is prewarm
    assert server._session_end_fnc is on_session_end
    assert server._session_end_timeout == 150  # 报告总时限 + 60 秒余量
    assert (server._num_idle_processes, server._drain_timeout) == (2, 1800)
    assert (server._ws_url, server._api_key, server._api_secret) == ("ws://x", "k", "s")


class FakeWall:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def message(text: str) -> llm.ChatMessage:
    return llm.ChatMessage(role="user", content=[text])


def make_agent() -> tuple[InterviewAgent, MagicMock, FakeWall]:
    wall = FakeWall()
    driver = MagicMock()
    agent = InterviewAgent(
        SessionClock(0, wall=wall), make_driver=lambda _speaker: driver, on_ended=lambda: None
    )
    agent._driver = driver  # pyright: ignore[reportPrivateUsage]  # on_enter 需要真实会话，这里直接装上
    return agent, driver, wall


def speaking(at: float) -> Any:
    return MagicMock(new_state="speaking", created_at=at)


async def test_final_transcript_goes_to_driver_with_speech_start() -> None:
    agent, driver, wall = make_agent()
    agent._on_user_state(speaking(101.5))  # pyright: ignore[reportPrivateUsage]
    agent._on_user_state(speaking(102.0))  # pyright: ignore[reportPrivateUsage]  # 同一轮只记第一次开口
    wall.now = 104.25
    await agent.on_user_turn_completed(llm.ChatContext.empty(), message(" 我做过支付系统。 "))
    driver.user_turn.assert_called_once_with("我做过支付系统。", 1500, 4250)
    # 下一轮没有开口事件时，开始时刻按结束时刻算
    wall.now = 110.0
    await agent.on_user_turn_completed(llm.ChatContext.empty(), message("还有缓存。"))
    driver.user_turn.assert_called_with("还有缓存。", 10_000, 10_000)


async def test_empty_transcript_is_ignored() -> None:
    agent, driver, _ = make_agent()
    agent._on_user_state(speaking(101.0))  # pyright: ignore[reportPrivateUsage]
    await agent.on_user_turn_completed(llm.ChatContext.empty(), message("  "))
    driver.user_turn.assert_not_called()


async def test_session_end_stops_driver_and_waits_for_report() -> None:
    agent = MagicMock(spec=InterviewAgent)
    agent.stop = AsyncMock()
    reports = MagicMock()
    reports.aclose = AsyncMock()
    ctx = MagicMock()
    ctx.proc.userdata = {"job": _JobState(agent=agent, reports=reports)}
    await on_session_end(ctx)
    agent.stop.assert_awaited_once()
    reports.aclose.assert_awaited_once()
    assert "job" not in ctx.proc.userdata
    await on_session_end(ctx)  # 没有任务（如 metadata 不合法时）什么都不做
