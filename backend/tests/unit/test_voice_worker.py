"""语音 worker：派发 metadata、AgentServer 配置、面试 agent 和会话收尾。"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from livekit.agents import llm
from livekit.agents.inference import TurnDetector
from pydantic import ValidationError
from structlog.testing import capture_logs
from test_api import make_settings

from interview.config import TurnSettings
from interview.db.models import Language
from interview.domain.state_machine import Directive, InterviewState, Phase
from interview.domain.turn_intents import default_keywords
from interview.voice.driver import SessionClock
from interview.voice.session import DispatchMetadata
from interview.voice.turn_policy import Endpointing
from interview.voice.worker import (
    InterviewAgent,
    TurnTaker,
    _JobState,  # pyright: ignore[reportPrivateUsage]
    build_server,
    entrypoint,
    on_session_end,
    prewarm,
    turn_handling,
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
        SessionClock(0, wall=wall),
        make_driver=lambda _speaker, _on_state: driver,
        on_ended=lambda: None,
        turn=TurnSettings.model_validate({}),
        keywords=default_keywords(),
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


def test_reply_latency_is_logged_from_when_the_candidate_stopped() -> None:
    agent, _, _ = make_agent()
    stopped = MagicMock(old_state="speaking", new_state="listening", created_at=200.0)
    agent._on_user_state(stopped)  # pyright: ignore[reportPrivateUsage]
    with capture_logs() as logs:
        agent._on_agent_state(MagicMock(new_state="speaking", created_at=200.85))  # pyright: ignore[reportPrivateUsage]
        agent._on_agent_state(MagicMock(new_state="speaking", created_at=203.0))  # pyright: ignore[reportPrivateUsage]
    replies = [e for e in logs if e["event"] == "voice_reply_started"]
    assert [e["since_user_stopped_ms"] for e in replies] == [850]  # 同一轮只记第一次开口


def test_turn_handling_starts_open_with_local_turn_detector() -> None:
    settings = make_settings()
    # 默认不在 0.5 秒的停顿处抢话；没说完时最多等 4 秒
    options = turn_handling(settings)
    assert options.get("endpointing") == {"min_delay": 1.5, "max_delay": 4.0}
    detector = options.get("turn_detection")
    # 本机推理的 v1-mini：不把候选人音频发去 LiveKit Cloud
    assert isinstance(detector, TurnDetector) and detector.model == "turn-detector-v1-mini"
    settings.turn.detector_enabled = False
    assert "turn_detection" not in turn_handling(settings)


def turn_state(kind: str, phase: Phase) -> InterviewState:
    directive = Directive.model_validate({"kind": kind, "phase": phase})
    return InterviewState.model_construct(phase=phase, last_directive=directive)


def test_turn_taker_switches_endpointing_only_on_change() -> None:
    applied: list[Endpointing] = []
    taker = TurnTaker(TurnSettings.model_validate({}), applied.append)
    with capture_logs() as logs:
        taker.update(turn_state("ask", Phase.TECHNICAL))  # 会话本来就是开放回答的参数
        taker.update(turn_state("check_in", Phase.TECHNICAL))
        taker.update(turn_state("pause", Phase.TECHNICAL))  # 还是短问题，不重复下发
        taker.update(turn_state("follow_up", Phase.TECHNICAL))
    assert applied == [Endpointing(0.6, 2.0), Endpointing(1.5, 4.0)]
    entries = [e for e in logs if e["event"] == "turn_policy_applied"]
    assert [(e["kind"], e["directive"]) for e in entries] == [
        ("short", "check_in"),
        ("open", "follow_up"),
    ]


def test_done_phrase_commits_the_turn_once() -> None:
    agent, _, _ = make_agent()
    session = MagicMock()
    agent._session = session  # pyright: ignore[reportPrivateUsage]
    transcript = MagicMock(transcript="我做过支付系统。", is_final=False)
    agent._on_transcript(transcript)  # pyright: ignore[reportPrivateUsage]
    session.commit_user_turn.assert_not_called()
    for text in ("……吞吐提升了三倍。就这些。", "就这些。"):
        agent._on_transcript(MagicMock(transcript=text, is_final=False))  # pyright: ignore[reportPrivateUsage]
    session.commit_user_turn.assert_called_once()  # 同一轮只提前结束一次
