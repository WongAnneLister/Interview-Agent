"""面试状态机：纯函数推进阶段、题目、追问、暂停和超时，不产生任何 IO。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from interview.domain.plan import InterviewPlan, Question


class Phase(StrEnum):
    OPENING = "opening"
    SELF_INTRO = "self_intro"
    RESUME_DEEP_DIVE = "resume_deep_dive"
    TECHNICAL = "technical"
    BEHAVIORAL = "behavioral"
    CANDIDATE_QUESTIONS = "candidate_questions"
    CLOSING = "closing"
    ENDED = "ended"


@dataclass(frozen=True)
class MachineConfig:
    max_follow_ups: int = 2
    think_timeout_ms: int = 20_000
    overtime_grace_ms: int = 30_000


DEFAULT = MachineConfig()

DirectiveKind = Literal[
    "opening",
    "ask",
    "follow_up",
    "stage_prompt",
    "converse",
    "check_in",
    "repeat",
    "pause",
    "resume",
    "closing",
]
Reason = Literal[
    "plan",
    "follow_up_limit",
    "stage_timeout",
    "total_timeout",
    "user_skip",
    "user_end",
    "wrap_up",
    "think_timeout",
]


class Directive(BaseModel):
    kind: DirectiveKind
    phase: Phase
    question_id: str | None = None
    question_text: str | None = None
    focus: str = ""
    transition_from: Phase | None = None
    reason: Reason | None = None
    repeat_of: str | None = None


class Tick(BaseModel):
    type: Literal["tick"] = "tick"
    now_ms: int


class Decision(BaseModel):
    type: Literal["decision"] = "decision"
    now_ms: int
    action: Literal["follow_up", "next_question", "next_stage", "wrap_up"]
    focus: str = ""


class Command(BaseModel):
    type: Literal["command"] = "command"
    now_ms: int
    command: Literal["pause", "resume", "repeat", "skip", "end"]


class Think(BaseModel):
    type: Literal["think"] = "think"
    now_ms: int


Event = Annotated[Tick | Decision | Command | Think, Field(discriminator="type")]


class InterviewState(BaseModel):
    schema_version: Literal[1] = 1
    phase: Phase
    stage_started_ms: int
    active_ms: int
    clock_ms: int
    current_question_id: str | None = None
    follow_ups: int = 0
    asked: list[str] = Field(default_factory=list[str])
    paused: bool = False
    thinking_since_ms: int | None = None
    last_directive: Directive | None = None

    @property
    def waiting_for_thought(self) -> bool:
        return self.thinking_since_ms is not None


class Step(BaseModel):
    state: InterviewState
    directive: Directive | None


def _update(state: InterviewState, **changes: Any) -> InterviewState:
    changes.setdefault("asked", [*state.asked])
    return state.model_copy(update=changes)


def _directive(kind: DirectiveKind, phase: Phase, **changes: Any) -> Directive:
    return Directive(kind=kind, phase=phase, **changes)


def _entered(
    state: InterviewState,
    phase: Phase | None,
    question_id: str | None,
    asked: list[str] | None = None,
    directive: Directive | None = None,
) -> InterviewState:
    return _update(
        state,
        current_question_id=question_id,
        follow_ups=0,
        thinking_since_ms=None,
        phase=state.phase if phase is None else phase,
        stage_started_ms=state.active_ms if phase is not None else state.stage_started_ms,
        asked=[*state.asked] if asked is None else asked,
        last_directive=state.last_directive if directive is None else directive,
    )


def _stage_deadline(plan: InterviewPlan, state: InterviewState) -> int | None:
    slot = next((slot for slot in plan.stages if slot.stage.value == state.phase.value), None)
    return None if slot is None else state.stage_started_ms + slot.seconds * 1000


def _total_deadline(plan: InterviewPlan) -> int:
    return sum(slot.seconds for slot in plan.stages) * 1000


def _next_phase(plan: InterviewPlan, phase: Phase) -> Phase:
    sequence = [*(Phase(slot.stage.value) for slot in plan.stages), Phase.ENDED]
    index = sequence.index(phase) if phase in sequence else len(sequence) - 1
    return sequence[min(index + 1, len(sequence) - 1)]


def _main_phase(plan: InterviewPlan) -> Phase:
    if any(slot.stage.value == Phase.TECHNICAL.value for slot in plan.stages):
        return Phase.TECHNICAL
    return Phase.BEHAVIORAL


def _is_question_phase(plan: InterviewPlan, phase: Phase) -> bool:
    return phase is Phase.RESUME_DEEP_DIVE or phase is _main_phase(plan)


def _first_unasked(plan: InterviewPlan, phase: Phase, asked: list[str]) -> Question | None:
    questions = (q for q in plan.questions if q.stage.value == phase.value and q.id not in asked)
    return next(questions, None)


def _question_text(plan: InterviewPlan, question_id: str | None) -> str | None:
    return next((question.text for question in plan.questions if question.id == question_id), None)


def _stage_timed_out(plan: InterviewPlan, state: InterviewState) -> bool:
    deadline = _stage_deadline(plan, state)
    return deadline is not None and state.active_ms >= deadline


def _stage_hard_timed_out(
    plan: InterviewPlan, state: InterviewState, config: MachineConfig
) -> bool:
    deadline = _stage_deadline(plan, state)
    return deadline is not None and state.active_ms > deadline + config.overtime_grace_ms


def _total_timed_out(plan: InterviewPlan, state: InterviewState) -> bool:
    return state.active_ms >= _total_deadline(plan)


def _total_hard_timed_out(
    plan: InterviewPlan, state: InterviewState, config: MachineConfig
) -> bool:
    return state.active_ms > _total_deadline(plan) + config.overtime_grace_ms


def _think_timed_out(state: InterviewState, config: MachineConfig) -> bool:
    return (
        state.thinking_since_ms is not None
        and state.active_ms - state.thinking_since_ms >= config.think_timeout_ms
    )


def _advance(state: InterviewState, now_ms: int) -> InterviewState:
    if state.paused:
        return _update(state, clock_ms=now_ms)
    active = state.active_ms + now_ms - state.clock_ms
    return _update(state, active_ms=active, clock_ms=now_ms)


def _enter(
    plan: InterviewPlan,
    state: InterviewState,
    phase: Phase,
    reason: Reason | None,
    from_phase: Phase,
) -> Step:
    if phase is Phase.ENDED:
        return Step(state=_entered(state, Phase.ENDED, None), directive=None)

    if _is_question_phase(plan, phase):
        question = _first_unasked(plan, phase, state.asked)
        if question is None:
            return _enter(plan, state, _next_phase(plan, phase), reason, from_phase)
        directive = _directive(
            "ask",
            phase,
            question_id=question.id,
            question_text=question.text,
            transition_from=from_phase,
            reason=reason,
        )
        new_state = _entered(
            state,
            phase,
            question.id,
            [*state.asked, question.id],
            directive,
        )
        return Step(state=new_state, directive=directive)

    kind: DirectiveKind = "stage_prompt"
    if phase is Phase.CLOSING:
        kind = "closing"
    elif phase is Phase.OPENING:
        kind = "opening"
    directive = _directive(kind, phase, transition_from=from_phase, reason=reason)
    return Step(state=_entered(state, phase, None, directive=directive), directive=directive)


def _next_question_or_stage(plan: InterviewPlan, state: InterviewState, reason: Reason) -> Step:
    if _stage_timed_out(plan, state):
        return _enter(plan, state, _next_phase(plan, state.phase), "stage_timeout", state.phase)
    question = _first_unasked(plan, state.phase, state.asked)
    if question is None:
        return _enter(plan, state, _next_phase(plan, state.phase), reason, state.phase)
    directive = _directive(
        "ask",
        state.phase,
        question_id=question.id,
        question_text=question.text,
        reason=reason,
    )
    new_state = _entered(
        state,
        None,
        question.id,
        [*state.asked, question.id],
        directive,
    )
    return Step(state=new_state, directive=directive)


def _tick(plan: InterviewPlan, state: InterviewState, config: MachineConfig) -> Step:
    if state.paused:
        return Step(state=state, directive=None)
    if state.phase is not Phase.CLOSING and _total_hard_timed_out(plan, state, config):
        return _enter(plan, state, Phase.CLOSING, "total_timeout", state.phase)
    if state.phase is Phase.CLOSING and _stage_hard_timed_out(plan, state, config):
        return _enter(plan, state, Phase.ENDED, None, state.phase)
    if state.phase is not Phase.CLOSING and _stage_hard_timed_out(plan, state, config):
        return _enter(plan, state, _next_phase(plan, state.phase), "stage_timeout", state.phase)
    if _think_timed_out(state, config):
        directive = _directive(
            "check_in",
            state.phase,
            question_id=state.current_question_id,
            question_text=_question_text(plan, state.current_question_id),
            reason="think_timeout",
        )
        new_state = _update(state, thinking_since_ms=None, last_directive=directive)
        return Step(state=new_state, directive=directive)
    return Step(state=state, directive=None)


def _decision(
    plan: InterviewPlan, state: InterviewState, event: Decision, config: MachineConfig
) -> Step:
    if state.paused:
        return Step(state=state, directive=None)
    current = _update(state, thinking_since_ms=None)
    if current.phase is Phase.CLOSING:
        return _enter(plan, current, Phase.ENDED, None, current.phase)
    if _total_timed_out(plan, current):
        return _enter(plan, current, Phase.CLOSING, "total_timeout", current.phase)
    if event.action == "wrap_up":
        return _enter(plan, current, Phase.CLOSING, "wrap_up", current.phase)
    if _stage_timed_out(plan, current):
        return _enter(
            plan, current, _next_phase(plan, current.phase), "stage_timeout", current.phase
        )
    if event.action == "next_stage":
        return _enter(plan, current, _next_phase(plan, current.phase), "plan", current.phase)
    if _is_question_phase(plan, current.phase):
        if event.action == "follow_up":
            if current.follow_ups < config.max_follow_ups:
                directive = _directive(
                    "follow_up",
                    current.phase,
                    question_id=current.current_question_id,
                    question_text=_question_text(plan, current.current_question_id),
                    focus=event.focus,
                )
                new_state = _update(
                    current,
                    follow_ups=current.follow_ups + 1,
                    last_directive=directive,
                )
                return Step(state=new_state, directive=directive)
            return _next_question_or_stage(plan, current, "follow_up_limit")
        return _next_question_or_stage(plan, current, "plan")
    if event.action == "follow_up":
        directive = _directive("converse", current.phase)
        new_state = _update(current, last_directive=directive)
        return Step(state=new_state, directive=directive)
    return _enter(plan, current, _next_phase(plan, current.phase), "plan", current.phase)


def _command(plan: InterviewPlan, state: InterviewState, event: Command) -> Step:
    if event.command == "pause":
        if state.phase is Phase.CLOSING or state.paused:
            return Step(state=state, directive=None)
        directive = _directive("pause", state.phase)
        new_state = _update(state, paused=True, thinking_since_ms=None)
        return Step(state=new_state, directive=directive)

    current = _update(state, thinking_since_ms=None)
    if event.command == "resume":
        if not current.paused:
            return Step(state=current, directive=None)
        last = current.last_directive
        directive = _directive(
            "resume",
            last.phase if last is not None else current.phase,
            question_id=last.question_id if last is not None else None,
            question_text=last.question_text if last is not None else None,
        )
        return Step(state=_update(current, paused=False), directive=directive)
    if event.command == "end":
        if current.phase is Phase.CLOSING:
            return _enter(plan, current, Phase.ENDED, None, current.phase)
        return _enter(
            plan,
            _update(current, paused=False),
            Phase.CLOSING,
            "user_end",
            current.phase,
        )
    if event.command == "repeat":
        if current.paused or current.last_directive is None:
            return Step(state=current, directive=None)
        last = current.last_directive
        directive = last.model_copy(update={"kind": "repeat", "repeat_of": last.kind})
        return Step(state=current, directive=directive)
    if current.paused or current.phase is Phase.CLOSING:
        return Step(state=current, directive=None)
    if _is_question_phase(plan, current.phase):
        return _next_question_or_stage(plan, current, "user_skip")
    return _enter(plan, current, _next_phase(plan, current.phase), "user_skip", current.phase)


def _think(state: InterviewState) -> Step:
    if state.phase is Phase.CLOSING or state.paused:
        return Step(state=state, directive=None)
    return Step(state=_update(state, thinking_since_ms=state.active_ms), directive=None)


def start(plan: InterviewPlan, now_ms: int, config: MachineConfig = DEFAULT) -> Step:
    directive = _directive("opening", Phase.OPENING)
    state = InterviewState(
        phase=Phase.OPENING,
        stage_started_ms=0,
        active_ms=0,
        clock_ms=now_ms,
        last_directive=directive,
    )
    return Step(state=state, directive=directive)


def step(
    plan: InterviewPlan,
    state: InterviewState,
    event: Event,
    config: MachineConfig = DEFAULT,
) -> Step:
    if state.phase is Phase.ENDED:
        return Step(state=state, directive=None)
    if event.now_ms < state.clock_ms:
        raise ValueError("now_ms 不能小于上一个事件的 now_ms")
    current = _advance(state, event.now_ms)
    if isinstance(event, Tick):
        return _tick(plan, current, config)
    if isinstance(event, Decision):
        return _decision(plan, current, event, config)
    if isinstance(event, Command):
        return _command(plan, current, event)
    return _think(current)


def stage_remaining_ms(plan: InterviewPlan, state: InterviewState) -> int:
    deadline = _stage_deadline(plan, state)
    return 0 if deadline is None else max(0, deadline - state.active_ms)


def total_remaining_ms(plan: InterviewPlan, state: InterviewState) -> int:
    return max(0, _total_deadline(plan) - state.active_ms)
