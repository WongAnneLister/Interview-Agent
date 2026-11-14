"""语音面试入房。

- POST /api/interviews/{id}/session：校验面试归属与状态，签发只能加入
  interview-{id} 房间的短时效 token，并在 token 里显式派发语音 agent。
  只有 planned（还没开始）和 in_progress（断线后重新入房）的面试可以入房。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from interview.api.deps import CurrentUserId, DbSession, get_resources
from interview.api.errors import ConflictError, NotFoundError
from interview.api.resources import Resources
from interview.db.models import InterviewStatus
from interview.domain.plan import InterviewRepository
from interview.voice.session import candidate_token

router = APIRouter(prefix="/api/interviews", tags=["voice"])
log = structlog.get_logger(__name__)

JOINABLE = frozenset({InterviewStatus.PLANNED, InterviewStatus.IN_PROGRESS})


class VoiceSessionOut(BaseModel):
    url: str
    token: str
    room: str
    identity: str
    expires_at: datetime


@router.post("/{interview_id}/session")
async def create_voice_session(
    interview_id: uuid.UUID,
    user_id: CurrentUserId,
    session: DbSession,
    resources: Annotated[Resources, Depends(get_resources)],
) -> VoiceSessionOut:
    interview = await InterviewRepository(session, user_id).get(interview_id)
    if interview is None or interview.user_id != user_id:
        raise NotFoundError("面试不存在")
    if interview.status == InterviewStatus.DRAFT:
        raise ConflictError("面试计划还没生成，不能开始语音面试")
    if interview.status not in JOINABLE:
        raise ConflictError("这场面试已经结束，不能再进入语音房间")
    issued = candidate_token(resources.settings.livekit, user_id=user_id, interview_id=interview_id)
    log.info(
        "voice_session_issued",
        interview_id=str(interview_id),
        room=issued.room,
        agent_name=resources.settings.livekit.agent_name,
    )
    return VoiceSessionOut(
        url=issued.url,
        token=issued.token,
        room=issued.room,
        identity=issued.identity,
        expires_at=issued.expires_at,
    )
