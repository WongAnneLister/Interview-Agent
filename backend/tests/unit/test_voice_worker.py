"""语音 worker：派发 metadata、AgentServer 配置和回声 agent。"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, PropertyMock

import pytest
from livekit.agents import llm
from pydantic import ValidationError
from test_api import make_settings

from interview.db.models import Language
from interview.voice.session import DispatchMetadata
from interview.voice.worker import GREETINGS, EchoAgent, build_server, entrypoint, prewarm

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
    server: Any = build_server(settings)
    assert server._agent_name == "interview-test"
    assert server._entrypoint_fnc is entrypoint
    assert server._setup_fnc is prewarm
    assert (server._num_idle_processes, server._drain_timeout) == (2, 1800)
    assert (server._ws_url, server._api_key, server._api_secret) == ("ws://x", "k", "s")


def message(text: str) -> llm.ChatMessage:
    return llm.ChatMessage(role="user", content=[text])


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock()
    monkeypatch.setattr(EchoAgent, "session", PropertyMock(return_value=fake))
    return fake


@pytest.mark.parametrize("language", list(Language))
async def test_echo_agent_greets_in_interview_language(
    session: MagicMock, language: Language
) -> None:
    await EchoAgent(language).on_enter()
    session.say.assert_called_once_with(GREETINGS[language])


async def test_echo_agent_repeats_what_was_heard(session: MagicMock) -> None:
    agent = EchoAgent(Language.ZH)
    await agent.on_user_turn_completed(llm.ChatContext.empty(), message(" 我做过支付系统。 "))
    session.say.assert_called_once_with("我做过支付系统。")


async def test_echo_agent_stays_silent_on_empty_transcript(session: MagicMock) -> None:
    await EchoAgent(Language.ZH).on_user_turn_completed(llm.ChatContext.empty(), message("  "))
    session.say.assert_not_called()
