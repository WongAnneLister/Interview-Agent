"""语音面试的轮次策略（纯逻辑）：按面试官上一句的类型，决定候选人停顿多久算说完。

- 确认类短问题（开场问“准备好了吗”、思考超时后的询问、暂停 / 继续、收尾、开场寒暄）：
  候选人通常一两个词就答完，等太久显得迟钝。
- 其余都是开放回答（提问、追问、引导进入新阶段、自我介绍和深挖里的接话）：
  中间停顿是正常的，0.5 秒就接话会抢话（PR-29 实测）。
- “请再说一遍”之后的重复按被重复的那句算。

参数（TURN_*）：min_delay 是判定说完后再等多久；max_delay 是轮次检测模型认为没说完时最多等多久。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from interview.config import TurnSettings
from interview.domain.state_machine import InterviewState, Phase

TurnKind = Literal["short", "open"]

_SHORT_DIRECTIVES = frozenset({"opening", "check_in", "pause", "resume", "closing"})
_SHORT_CONVERSE_PHASES = frozenset({Phase.OPENING, Phase.CLOSING})


@dataclass(frozen=True, slots=True)
class Endpointing:
    min_delay: float
    max_delay: float


def classify(state: InterviewState | None) -> TurnKind:
    directive = state.last_directive if state is not None else None
    if state is None or directive is None:
        return "open"
    kind = (
        directive.repeat_of
        if directive.kind == "repeat" and directive.repeat_of
        else directive.kind
    )
    if kind in _SHORT_DIRECTIVES:
        return "short"
    if kind == "converse" and state.phase in _SHORT_CONVERSE_PHASES:
        return "short"
    return "open"


def endpointing_for(kind: TurnKind, settings: TurnSettings) -> Endpointing:
    if kind == "short":
        return Endpointing(settings.short_min_delay_s, settings.short_max_delay_s)
    return Endpointing(settings.open_min_delay_s, settings.open_max_delay_s)
