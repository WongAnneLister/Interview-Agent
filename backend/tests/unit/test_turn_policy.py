"""轮次策略：按面试官上一句的类型给出停顿参数。"""

from __future__ import annotations

import pytest

from interview.config import TurnSettings
from interview.domain.state_machine import Directive, DirectiveKind, InterviewState, Phase
from interview.voice.turn_policy import Endpointing, TurnKind, classify, endpointing_for


def state(
    kind: DirectiveKind | None, phase: Phase, *, repeat_of: str | None = None
) -> InterviewState:
    directive = Directive(kind=kind, phase=phase, repeat_of=repeat_of) if kind is not None else None
    return InterviewState.model_construct(phase=phase, last_directive=directive)


@pytest.mark.parametrize(
    ("kind", "phase", "expected"),
    [
        ("opening", Phase.OPENING, "short"),  # “准备好了吗”
        ("converse", Phase.OPENING, "short"),  # 开场寒暄
        ("check_in", Phase.TECHNICAL, "short"),  # 思考超时后的询问
        ("pause", Phase.TECHNICAL, "short"),
        ("resume", Phase.TECHNICAL, "short"),
        ("closing", Phase.CLOSING, "short"),
        ("ask", Phase.TECHNICAL, "open"),
        ("follow_up", Phase.RESUME_DEEP_DIVE, "open"),
        ("stage_prompt", Phase.SELF_INTRO, "open"),  # 请做自我介绍
        ("converse", Phase.SELF_INTRO, "open"),  # 自我介绍中途的“请继续”
        ("converse", Phase.CANDIDATE_QUESTIONS, "open"),
    ],
)
def test_classify_by_last_directive(kind: DirectiveKind, phase: Phase, expected: TurnKind) -> None:
    assert classify(state(kind, phase)) == expected


def test_repeat_counts_as_what_was_repeated() -> None:
    assert classify(state("repeat", Phase.TECHNICAL, repeat_of="ask")) == "open"
    assert classify(state("repeat", Phase.OPENING, repeat_of="opening")) == "short"


def test_no_state_or_directive_is_open() -> None:
    assert classify(None) == "open"
    assert classify(state(None, Phase.OPENING)) == "open"


def test_endpointing_values_come_from_settings() -> None:
    settings = TurnSettings.model_validate({})
    assert endpointing_for("short", settings) == Endpointing(0.6, 2.0)
    assert endpointing_for("open", settings) == Endpointing(1.5, 4.0)
    custom = TurnSettings.model_validate({"open_min_delay_s": 2.0, "open_max_delay_s": 5.0})
    assert endpointing_for("open", custom) == Endpointing(2.0, 5.0)


def test_max_below_min_is_rejected() -> None:
    with pytest.raises(ValueError, match="TURN_OPEN_MAX_DELAY_S"):
        TurnSettings.model_validate({"open_min_delay_s": 3.0, "open_max_delay_s": 2.0})
