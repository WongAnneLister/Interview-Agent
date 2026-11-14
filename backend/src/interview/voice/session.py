"""候选人加入语音面试房间的 token。

- 房间名 interview-{id}，一场面试一个房间。
- token 只授予加入这一个房间的权限：不能建房、列房、管理房间或录制，
  只能发布麦克风（不能发布摄像头和屏幕共享），可以订阅和收发数据消息。
- 显式派发：token 的 room_config 里带一个 RoomAgentDispatch，房间由这个 token
  首次创建时，LiveKit 按 agent_name 派发语音 worker，metadata 里是 interview_id。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from livekit.api import AccessToken, VideoGrants
from livekit.protocol.agent_dispatch import RoomAgentDispatch
from livekit.protocol.room import RoomConfiguration

from interview.config import LiveKitSettings

# 候选人只能发布麦克风；LiveKit 的 TrackSource 名称
CANDIDATE_SOURCES = ["microphone"]


def room_name(interview_id: uuid.UUID) -> str:
    return f"interview-{interview_id}"


def candidate_identity(user_id: uuid.UUID) -> str:
    return f"candidate-{user_id}"


def dispatch_metadata(interview_id: uuid.UUID) -> str:
    """派发给 worker 的 metadata（JSON 字符串）；worker 从中读取 interview_id。"""
    return json.dumps({"interview_id": str(interview_id)})


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
                metadata=dispatch_metadata(interview_id),
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
