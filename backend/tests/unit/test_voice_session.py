"""语音入房 token：权限范围、显式派发与越权。"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from livekit.api import TokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlite_helpers import create_tables
from test_api import error_of, make_settings, open_client
from test_orchestrator import INTERVIEW_ID, USER, make_plan, seed

from interview.api.app import create_app
from interview.api.deps import routes_without_user
from interview.api.resources import Resources
from interview.config import LiveKitSettings, Settings
from interview.db.models import Interview, InterviewStatus, Language, Plan, Resume, Turn, User
from interview.db.session import make_sessionmaker
from interview.domain.plan import InterviewRepository
from interview.providers.registry import ProviderRegistry
from interview.voice.session import DispatchMetadata, candidate_token

URL = f"/api/interviews/{INTERVIEW_ID}/session"


def livekit(**overrides: object) -> LiveKitSettings:
    values: dict[str, object] = {
        "url": "ws://127.0.0.1:7880",
        "api_key": "devkey",
        "api_secret": "a-test-secret-that-is-long-enough-for-hs256",
    }
    values.update(overrides)
    return LiveKitSettings.model_validate(values)


def verify(settings: LiveKitSettings) -> TokenVerifier:
    return TokenVerifier(
        settings.api_key.get_secret_value(), settings.api_secret.get_secret_value()
    )


def test_token_only_joins_this_interview_room() -> None:
    settings = livekit()
    issued = candidate_token(
        settings, user_id=USER, interview_id=INTERVIEW_ID, language=Language.ZH
    )
    claims = verify(settings).verify(issued.token)

    assert issued.room == f"interview-{INTERVIEW_ID}"
    assert claims.identity == issued.identity == f"candidate-{USER}"
    assert claims.kind == "standard"
    video = claims.video
    assert video is not None
    assert video.room_join is True
    assert video.room == issued.room
    # 不能建房、列房、管理房间、录制或以隐藏身份进入
    assert not video.room_create
    assert not video.room_list
    assert not video.room_admin
    assert not video.room_record
    assert not video.hidden
    assert not video.agent
    assert not video.can_update_own_metadata
    # 只能发布麦克风
    assert video.can_publish_sources == ["microphone"]
    # 没有其它种类的权限
    assert claims.sip is None
    assert claims.agent is None
    assert claims.inference is None


def test_token_dispatches_agent_with_interview_id() -> None:
    settings = livekit(agent_name="interview-test")
    issued = candidate_token(
        settings, user_id=USER, interview_id=INTERVIEW_ID, language=Language.ZH
    )
    config = verify(settings).verify(issued.token).room_config

    assert config is not None
    assert config.name == issued.room
    assert [a.agent_name for a in config.agents] == ["interview-test"]
    assert json.loads(config.agents[0].metadata) == {
        "interview_id": str(INTERVIEW_ID),
        "user_id": str(USER),
        "language": "zh",
    }
    assert DispatchMetadata.parse(config.agents[0].metadata) == DispatchMetadata(
        interview_id=INTERVIEW_ID, user_id=USER, language=Language.ZH
    )


def test_token_expires_after_configured_ttl() -> None:
    settings = livekit(token_ttl_s=600)
    now = datetime.now(UTC)
    issued = candidate_token(
        settings, user_id=USER, interview_id=INTERVIEW_ID, language=Language.ZH, now=now
    )
    assert issued.expires_at == now + timedelta(seconds=600)

    payload = jwt.decode(issued.token, options={"verify_signature": False})
    assert 590 <= payload["exp"] - payload["nbf"] <= 610


def test_token_signed_with_other_secret_is_rejected() -> None:
    issued = candidate_token(
        livekit(), user_id=USER, interview_id=INTERVIEW_ID, language=Language.ZH
    )
    other = livekit(api_secret="another-secret-that-is-long-enough-for-hs256")
    with pytest.raises(jwt.InvalidSignatureError):
        verify(other).verify(issued.token)


@dataclass
class Api:
    client: httpx.AsyncClient
    sessionmaker: async_sessionmaker[AsyncSession]
    settings: Settings

    async def status(self, status: InterviewStatus) -> None:
        async with self.sessionmaker() as session:
            row = await InterviewRepository(session, USER).get(INTERVIEW_ID)
            assert row is not None
            row.status = status
            await session.commit()


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await create_tables(conn, User, Resume, Interview, Plan, Turn)
    sm = make_sessionmaker(engine)
    await seed(sm, make_plan())
    settings = make_settings(user=USER)
    settings.livekit = livekit()
    resources = Resources(
        settings=settings, sessionmaker=sm, providers=ProviderRegistry(settings.providers)
    )
    app = create_app(settings, resource_factory=lambda _s: resources, init_observability=False)
    assert routes_without_user(app) == []
    async with open_client(app) as client:
        api = Api(client, sm, settings)
        await api.status(InterviewStatus.PLANNED)
        yield api
    await engine.dispose()


@pytest.mark.parametrize("status", [InterviewStatus.PLANNED, InterviewStatus.IN_PROGRESS])
async def test_session_issues_token_for_joinable_interview(
    api: Api, status: InterviewStatus
) -> None:
    await api.status(status)
    response = await api.client.post(URL)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"url", "token", "room", "identity", "expires_at"}
    assert body["url"] == "ws://127.0.0.1:7880"
    assert body["room"] == f"interview-{INTERVIEW_ID}"
    claims = verify(api.settings.livekit).verify(body["token"])
    assert claims.video is not None and claims.video.room == body["room"]
    assert claims.room_config is not None
    metadata = DispatchMetadata.parse(claims.room_config.agents[0].metadata)
    assert metadata == DispatchMetadata(
        interview_id=INTERVIEW_ID, user_id=USER, language=Language.ZH
    )


@pytest.mark.parametrize("other_user", [False, True])
async def test_session_missing_or_other_user_is_404(api: Api, other_user: bool) -> None:
    interview_id = INTERVIEW_ID if other_user else uuid.uuid4()
    if other_user:
        api.settings.app.default_user_id = uuid.uuid4()
    response = await api.client.post(f"/api/interviews/{interview_id}/session")
    assert response.status_code == 404
    assert error_of(response)["message"] == "面试不存在"


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (InterviewStatus.DRAFT, "面试计划还没生成，不能开始语音面试"),
        (InterviewStatus.COMPLETED, "这场面试已经结束，不能再进入语音房间"),
        (InterviewStatus.ABORTED, "这场面试已经结束，不能再进入语音房间"),
        (InterviewStatus.FAILED, "这场面试已经结束，不能再进入语音房间"),
    ],
)
async def test_session_rejects_unjoinable_interview(
    api: Api, status: InterviewStatus, message: str
) -> None:
    await api.status(status)
    response = await api.client.post(URL)
    assert response.status_code == 409
    assert error_of(response)["message"] == message
