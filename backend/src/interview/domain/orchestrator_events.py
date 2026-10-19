"""编排器吐给传输层（文字 WebSocket、语音 worker）的事件和配置。

事件用 type 字段区分，PR-20 会把它们导出给前端。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from interview.domain.state_machine import DEFAULT, MachineConfig, Phase


class OrchestratorConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    eval_wait_s: float = 2.5  # 从启动评估算起，最多等多久
    machine: MachineConfig = DEFAULT


DEFAULT_CONFIG = OrchestratorConfig()

type CommandName = Literal["pause", "resume", "repeat", "skip", "end"]


class InterviewerDelta(BaseModel):
    type: Literal["interviewer_delta"] = "interviewer_delta"
    text: str


class InterviewerSentence(BaseModel):
    type: Literal["interviewer_sentence"] = "interviewer_sentence"
    text: str
    transition: bool = False


class InterviewerDone(BaseModel):
    type: Literal["interviewer_done"] = "interviewer_done"
    turn_id: uuid.UUID
    text: str


class StateEvent(BaseModel):
    type: Literal["state"] = "state"
    value: Literal["listening", "thinking", "speaking", "paused"]


class StageEvent(BaseModel):
    type: Literal["stage"] = "stage"
    phase: Phase
    remaining_s: int
    total_remaining_s: int


class EndedEvent(BaseModel):
    type: Literal["ended"] = "ended"


OrchestratorEvent = Annotated[
    InterviewerDelta | InterviewerSentence | InterviewerDone | StateEvent | StageEvent | EndedEvent,
    Field(discriminator="type"),
]
