"""候选人加入语音面试房间的 token。

- 房间名 interview-{id}，一场面试一个房间。
- token 只授予加入这一个房间的权限：不能建房、列房、管理房间或录制，
  只能发布麦克风（不能发布摄像头和屏幕共享），可以订阅和收发数据消息。
- 显式派发：token 的 room_config 里带一个 RoomAgentDispatch，房间由这个 token
  首次创建时，LiveKit 按 agent_name 派发语音 worker，metadata 是 DispatchMetadata 的 JSON。
  token 由我们的密钥签名，worker 可以信任这份 metadata。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from livekit.api import AccessToken, VideoGrants
from livekit.protocol.agent_dispatch import RoomAgentDispatch
from livekit.protocol.room import RoomConfiguration
from pydantic import BaseModel, ConfigDict

from interview.config import LiveKitSettings
from interview.db.models import Language

# 候选人只能发布麦克风；LiveKit 的 TrackSource 名称
CANDIDATE_SOURCES = ["microphone"]


def room_name(interview_id: uuid.UUID) -> str:
    return f"interview-{interview_id}"


def candidate_identity(user_id: uuid.UUID) -> str:
    return f"candidate-{user_id}"


class DispatchMetadata(BaseModel):
    """API 派发语音 worker 时带的信息；worker 不查库就能开始（用量要 user_id，插件要语言）。"""

    model_config = ConfigDict(frozen=True)

    interview_id: uuid.UUID
    user_id: uuid.UUID
    language: Language

    @classmethod
    def parse(cls, raw: str) -> DispatchMetadata:
        """解析 worker 收到的 metadata；不合法时抛 pydantic.ValidationError。"""
        return cls.model_validate_json(raw)


@dataclass(frozen=True, slots=True)
class CandidateToken:
    url: str
    token: str
    room: str
    identity: str
    expires_at: datetime


def candidate_token(
    settings: LiveKitSettings,
    *,
    user_id: uuid.UUID,
    interview_id: uuid.UUID,
    language: Language,
    now: datetime | None = None,
) -> CandidateToken:
    """签发候选人加入本场面试房间的短时效 token。"""
    room = room_name(interview_id)
    identity = candidate_identity(user_id)
    ttl = timedelta(seconds=settings.token_ttl_s)
    grants = VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_publish_sources=list(CANDIDATE_SOURCES),
        can_subscribe=True,
        can_publish_data=True,
        can_update_own_metadata=False,
    )
    config = RoomConfiguration(
        name=room,
        agents=[
            RoomAgentDispatch(
                agent_name=settings.agent_name,
                metadata=DispatchMetadata(
                    interview_id=interview_id, user_id=user_id, language=language
                ).model_dump_json(),
            )
        ],
    )
    token = (
        AccessToken(
            settings.api_key.get_secret_value(),
            settings.api_secret.get_secret_value(),
        )
        .with_identity(identity)
        .with_kind("standard")
        .with_ttl(ttl)
        .with_grants(grants)
        .with_room_config(config)
        .to_jwt()
    )
    issued = now or datetime.now(UTC)
    return CandidateToken(
        url=settings.url,
        token=token,
        room=room,
        identity=identity,
        expires_at=issued + ttl,
    )
