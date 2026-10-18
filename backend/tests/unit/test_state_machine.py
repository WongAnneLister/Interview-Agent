"""面试状态机：转移表、时间规则、序列化和 hypothesis 随机序列。"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from interview.db.models import InterviewType
from interview.domain.plan import InterviewPlan, Question, Stage, allocate_stages
from interview.domain.state_machine import (
    DEFAULT,
    Command,
    Decision,
    Directive,
    Event,
    InterviewState,
    Phase,
    Think,
    Tick,
    stage_remaining_ms,
    start,
    step,
    total_remaining_ms,
)

MAX_FOLLOW_UPS = DEFAULT.max_follow_ups


def make_plan(
    duration_min: int = 20,
    interview_type: InterviewType = InterviewType.TECHNICAL,
    *,
    deep_questions: int = 2,
    main_questions: int = 2,
) -> InterviewPlan:
    stages = allocate_stages(duration_min, interview_type)
    main_stage = Stage.BEHAVIORAL if interview_type is InterviewType.BEHAVIORAL else Stage.TECHNICAL
    questions: list[Question] = []
    for index in range(1, deep_questions + 1):
        questions.append(
            Question(
                id=f"q{index}",
                stage=Stage.RESUME_DEEP_DIVE,
                competency="能力",
                project="",
                text=f"题q{index}",
                key_points=["要点"],
                follow_ups=["追问一", "追问二"],
            )
        )
    for offset in range(1, main_questions + 1):
        index = deep_questions + offset
        questions.append(
            Question(
                id=f"q{index}",
                stage=main_stage,
                competency="能力",
                project="",
                text=f"题q{index}",
                key_points=["要点"],
                follow_ups=["追问一", "追问二"],
            )
        )
    return InterviewPlan(
        stages=stages,
        competencies=[],
        questions=questions,
        tech_terms=[],
        prompt_version="plan_questions@1",
    )


PLAN = make_plan()
EMPTY_DEEP_PLAN = make_plan(deep_questions=0)
BEHAVIORAL_PLAN = make_plan(30, InterviewType.BEHAVIORAL)


def stage_start(plan: InterviewPlan, phase: Phase) -> int:
    elapsed = 0
    for slot in plan.stages:
        if slot.stage.value == phase.value:
            return elapsed
        elapsed += slot.seconds * 1000
    return elapsed


def total_ms(plan: InterviewPlan) -> int:
    return sum(slot.seconds for slot in plan.stages) * 1000


def make_state(
    phase: Phase,
    *,
    stage_started_ms: int = 0,
    active_ms: int = 0,
    clock_ms: int | None = None,
    current_question_id: str | None = None,
    follow_ups: int = 0,
    asked: list[str] | None = None,
    paused: bool = False,
    thinking_since_ms: int | None = None,
    last_directive: Directive | None = None,
) -> InterviewState:
    return InterviewState(
        phase=phase,
        stage_started_ms=stage_started_ms,
        active_ms=active_ms,
        clock_ms=active_ms if clock_ms is None else clock_ms,
        current_question_id=current_question_id,
        follow_ups=follow_ups,
        asked=[] if asked is None else asked,
        paused=paused,
        thinking_since_ms=thinking_since_ms,
        last_directive=last_directive,
    )


def state_at(
    plan: InterviewPlan,
    phase: Phase,
    *,
    after_ms: int = 20_000,
    **kwargs: Any,
) -> InterviewState:
    started = stage_start(plan, phase)
    return make_state(
        phase,
        stage_started_ms=started,
        active_ms=started + after_ms,
        **kwargs,
    )


ASK_Q1 = Directive(
    kind="ask",
    phase=Phase.RESUME_DEEP_DIVE,
    question_id="q1",
    question_text="题q1",
    transition_from=Phase.SELF_INTRO,
    reason="plan",
)


def deep_state(plan: InterviewPlan = PLAN, **kwargs: Any) -> InterviewState:
    return state_at(
        plan,
        Phase.RESUME_DEEP_DIVE,
        current_question_id="q1",
        asked=["q1"],
        last_directive=ASK_Q1,
        **kwargs,
    )


# ---- start、时间与结束 ----


def test_start_opens_interview() -> None:
    result = start(PLAN, now_ms=1_234)
    assert result.state.phase is Phase.OPENING
    assert result.state.active_ms == 0
    assert result.state.clock_ms == 1_234
    assert result.state.stage_started_ms == 0
    assert result.state.asked == []
    assert result.directive is not None
    assert result.directive.kind == "opening"
    assert result.directive.phase is Phase.OPENING
    assert result.state.last_directive == result.directive


def test_time_regression_raises() -> None:
    state = start(PLAN, now_ms=100).state
    with pytest.raises(ValueError, match="now_ms"):
        step(PLAN, state, Tick(now_ms=99))


@pytest.mark.parametrize(
    "event",
    [
        Tick(now_ms=10),
        Decision(now_ms=10, action="next_question"),
        Command(now_ms=10, command="pause"),
        Think(now_ms=10),
    ],
)
def test_ended_state_ignores_every_event(event: Event) -> None:
    ended = make_state(Phase.ENDED, active_ms=1_000, clock_ms=1_000)
    result = step(PLAN, ended, event)
    assert result.state == ended
    assert result.directive is None


def test_stage_and_total_remaining() -> None:
    state = deep_state()
    assert stage_remaining_ms(PLAN, state) == (
        stage_start(PLAN, Phase.RESUME_DEEP_DIVE) + 360_000 - state.active_ms
    )
    assert total_remaining_ms(PLAN, state) == total_ms(PLAN) - state.active_ms
    late = make_state(Phase.CLOSING, active_ms=total_ms(PLAN) + 1)
    assert stage_remaining_ms(PLAN, late) == 0
    assert total_remaining_ms(PLAN, late) == 0


# ---- Tick ----


def test_tick_ignored_while_paused_and_stops_the_clock() -> None:
    state = make_state(Phase.SELF_INTRO, active_ms=1_000, paused=True, thinking_since_ms=900)
    result = step(PLAN, state, Tick(now_ms=999_999))
    assert result.directive is None
    assert result.state.active_ms == 1_000
    assert result.state.clock_ms == 999_999
    assert result.state.thinking_since_ms == 900


def test_tick_total_hard_timeout_enters_closing() -> None:
    state = make_state(
        Phase.SELF_INTRO,
        stage_started_ms=60_000,
        active_ms=total_ms(PLAN) + DEFAULT.overtime_grace_ms + 1,
    )
    result = step(PLAN, state, Tick(now_ms=state.clock_ms))
    assert result.state.phase is Phase.CLOSING
    assert result.directive is not None
    assert (result.directive.kind, result.directive.reason) == ("closing", "total_timeout")
    assert result.directive.transition_from is Phase.SELF_INTRO


def test_tick_closing_hard_timeout_ends() -> None:
    started = stage_start(PLAN, Phase.CLOSING)
    closing_ms = 60_000
    state = make_state(
        Phase.CLOSING,
        stage_started_ms=started,
        active_ms=started + closing_ms + DEFAULT.overtime_grace_ms + 1,
    )
    result = step(PLAN, state, Tick(now_ms=state.clock_ms))
    assert result.state.phase is Phase.ENDED
    assert result.directive is None
    assert result.state.current_question_id is None


def test_tick_other_stage_hard_timeout_advances() -> None:
    state = make_state(
        Phase.SELF_INTRO,
        stage_started_ms=60_000,
        active_ms=60_000 + 120_000 + DEFAULT.overtime_grace_ms + 1,
    )
    result = step(PLAN, state, Tick(now_ms=state.clock_ms))
    assert result.state.phase is Phase.RESUME_DEEP_DIVE
    assert result.directive is not None
    assert (result.directive.kind, result.directive.reason) == ("ask", "stage_timeout")
    assert result.directive.transition_from is Phase.SELF_INTRO


def test_tick_think_timeout_checks_in() -> None:
    state = deep_state(thinking_since_ms=180_000)
    result = step(PLAN, state, Tick(now_ms=200_000))
    assert result.state.thinking_since_ms is None
    assert result.directive is not None
    assert (result.directive.kind, result.directive.reason) == ("check_in", "think_timeout")
    assert result.directive.question_id == "q1"
    assert result.directive.question_text == "题q1"
    assert result.state.last_directive == result.directive


def test_tick_otherwise_ignored() -> None:
    state = make_state(Phase.SELF_INTRO, stage_started_ms=60_000, active_ms=100_000)
    result = step(PLAN, state, Tick(now_ms=101_000))
    assert result.directive is None
    assert result.state.active_ms == 101_000
    assert result.state.clock_ms == 101_000


# ---- Decision ----


def test_decision_ignored_while_paused() -> None:
    state = make_state(Phase.SELF_INTRO, active_ms=1_000, paused=True)
    result = step(PLAN, state, Decision(now_ms=5_000, action="next_question"))
    assert result.directive is None
    assert result.state.phase is Phase.SELF_INTRO
    assert result.state.active_ms == 1_000
    assert result.state.clock_ms == 5_000


def test_decision_in_closing_ends() -> None:
    state = state_at(PLAN, Phase.CLOSING)
    result = step(PLAN, state, Decision(now_ms=state.clock_ms, action="next_question"))
    assert result.state.phase is Phase.ENDED
    assert result.directive is None


def test_decision_total_timeout_enters_closing() -> None:
    state = make_state(
        Phase.SELF_INTRO,
        stage_started_ms=60_000,
        active_ms=total_ms(PLAN),
    )
    result = step(PLAN, state, Decision(now_ms=state.clock_ms, action="next_question"))
    assert result.state.phase is Phase.CLOSING
    assert result.directive is not None
    assert (result.directive.kind, result.directive.reason) == ("closing", "total_timeout")


def test_decision_stage_timeout_advances() -> None:
    state = make_state(
        Phase.RESUME_DEEP_DIVE,
        stage_started_ms=180_000,
        active_ms=180_000 + 360_000,
        current_question_id="q1",
        asked=["q1"],
        last_directive=ASK_Q1,
    )
    result = step(PLAN, state, Decision(now_ms=state.clock_ms, action="follow_up"))
    assert result.state.phase is Phase.TECHNICAL
    assert result.directive is not None
    assert (result.directive.kind, result.directive.reason) == ("ask", "stage_timeout")
    assert result.directive.transition_from is Phase.RESUME_DEEP_DIVE


def test_decision_wrap_up_enters_closing() -> None:
    result = step(PLAN, deep_state(), Decision(now_ms=200_000, action="wrap_up"))
    assert result.state.phase is Phase.CLOSING
    assert result.directive is not None
    assert (result.directive.kind, result.directive.reason) == ("closing", "wrap_up")


def test_decision_wrap_up_beats_stage_timeout() -> None:
    state = make_state(
        Phase.RESUME_DEEP_DIVE,
        stage_started_ms=180_000,
        active_ms=180_000 + 360_000,
        current_question_id="q1",
        asked=["q1"],
        last_directive=ASK_Q1,
    )
    result = step(PLAN, state, Decision(now_ms=state.clock_ms, action="wrap_up"))
    assert result.state.phase is Phase.CLOSING
    assert result.directive is not None
    assert (result.directive.kind, result.directive.reason) == ("closing", "wrap_up")


def test_decision_next_stage_enters_next_phase() -> None:
    state = state_at(PLAN, Phase.SELF_INTRO)
    result = step(PLAN, state, Decision(now_ms=state.clock_ms, action="next_stage"))
    assert result.state.phase is Phase.RESUME_DEEP_DIVE
    assert result.directive is not None
    assert (result.directive.kind, result.directive.reason) == ("ask", "plan")


def test_decision_follow_up_increments() -> None:
    result = step(
        PLAN,
        deep_state(),
        Decision(now_ms=200_000, action="follow_up", focus="性能"),
    )
    assert result.state.phase is Phase.RESUME_DEEP_DIVE
    assert result.state.follow_ups == 1
    assert result.directive is not None
    assert result.directive.kind == "follow_up"
    assert (result.directive.question_id, result.directive.focus) == ("q1", "性能")
    assert result.directive.question_text == "题q1"
    assert result.state.last_directive == result.directive


def test_decision_follow_up_at_limit_moves_to_next_question() -> None:
    state = deep_state(follow_ups=MAX_FOLLOW_UPS)
    result = step(
        PLAN,
        state,
        Decision(now_ms=state.clock_ms, action="follow_up"),
    )
    assert result.state.phase is Phase.RESUME_DEEP_DIVE
    assert result.state.current_question_id == "q2"
    assert result.state.follow_ups == 0
    assert result.directive is not None
    assert (result.directive.kind, result.directive.reason) == ("ask", "follow_up_limit")


def test_decision_next_question_asks_next_question() -> None:
    result = step(
        PLAN,
        deep_state(follow_ups=1),
        Decision(now_ms=200_000, action="next_question"),
    )
    assert result.state.current_question_id == "q2"
    assert result.state.follow_ups == 0
    assert result.directive is not None
    assert (result.directive.kind, result.directive.reason) == ("ask", "plan")


def test_decision_follow_up_in_non_question_phase_converses() -> None:
    state = state_at(PLAN, Phase.SELF_INTRO)
    result = step(
        PLAN,
        state,
        Decision(now_ms=state.clock_ms, action="follow_up", focus="经历"),
    )
    assert result.state.phase is Phase.SELF_INTRO
    assert result.directive is not None
    assert result.directive.kind == "converse"
    assert result.directive.focus == ""
    assert result.state.last_directive == result.directive


def test_decision_next_question_in_non_question_phase_advances() -> None:
    state = state_at(PLAN, Phase.SELF_INTRO)
    result = step(PLAN, state, Decision(now_ms=state.clock_ms, action="next_question"))
    assert result.state.phase is Phase.RESUME_DEEP_DIVE
    assert result.directive is not None
    assert (result.directive.kind, result.directive.reason) == ("ask", "plan")


def test_decision_clears_thinking() -> None:
    state = deep_state(thinking_since_ms=190_000)
    result = step(PLAN, state, Decision(now_ms=200_000, action="follow_up"))
    assert result.state.thinking_since_ms is None


# ---- Command ----


def test_pause_ignored_in_closing() -> None:
    state = state_at(PLAN, Phase.CLOSING)
    result = step(PLAN, state, Command(now_ms=state.clock_ms, command="pause"))
    assert result.directive is None
    assert result.state.paused is False


def test_pause_ignored_when_already_paused() -> None:
    state = make_state(Phase.SELF_INTRO, active_ms=1_000, paused=True)
    result = step(PLAN, state, Command(now_ms=1_000, command="pause"))
    assert result.directive is None
    assert result.state.paused is True


def test_pause_clears_thinking_and_keeps_last_directive() -> None:
    state = deep_state(thinking_since_ms=190_000)
    result = step(PLAN, state, Command(now_ms=200_000, command="pause"))
    assert result.state.paused is True
    assert result.state.thinking_since_ms is None
    assert result.state.last_directive == ASK_Q1
    assert result.directive is not None
    assert result.directive.kind == "pause"


def test_resume_ignored_when_not_paused_but_clears_thinking() -> None:
    state = deep_state(thinking_since_ms=190_000)
    result = step(PLAN, state, Command(now_ms=200_000, command="resume"))
    assert result.directive is None
    assert result.state.paused is False
    assert result.state.thinking_since_ms is None


def test_resume_restores_last_question() -> None:
    state = deep_state(paused=True)
    result = step(PLAN, state, Command(now_ms=state.clock_ms, command="resume"))
    assert result.state.paused is False
    assert result.directive is not None
    assert result.directive.kind == "resume"
    assert result.directive.phase is Phase.RESUME_DEEP_DIVE
    assert (result.directive.question_id, result.directive.question_text) == ("q1", "题q1")


def test_end_in_closing_ends() -> None:
    state = state_at(PLAN, Phase.CLOSING)
    result = step(PLAN, state, Command(now_ms=state.clock_ms, command="end"))
    assert result.state.phase is Phase.ENDED
    assert result.directive is None


def test_end_while_paused_enters_closing() -> None:
    state = deep_state(paused=True)
    result = step(PLAN, state, Command(now_ms=state.clock_ms, command="end"))
    assert result.state.phase is Phase.CLOSING
    assert result.state.paused is False
    assert result.directive is not None
    assert (result.directive.kind, result.directive.reason) == ("closing", "user_end")


def test_repeat_ignored_while_paused() -> None:
    state = deep_state(paused=True)
    result = step(PLAN, state, Command(now_ms=state.clock_ms, command="repeat"))
    assert result.directive is None
    assert result.state.phase is Phase.RESUME_DEEP_DIVE


def test_repeat_copies_last_directive() -> None:
    result = step(PLAN, deep_state(), Command(now_ms=200_000, command="repeat"))
    assert result.directive is not None
    assert result.directive.kind == "repeat"
    assert result.directive.repeat_of == "ask"
    assert (result.directive.question_id, result.directive.question_text) == ("q1", "题q1")
    assert result.state.last_directive == ASK_Q1


def test_skip_ignored_while_paused() -> None:
    state = deep_state(paused=True)
    result = step(PLAN, state, Command(now_ms=state.clock_ms, command="skip"))
    assert result.directive is None
    assert result.state.phase is Phase.RESUME_DEEP_DIVE


def test_skip_ignored_in_closing() -> None:
    state = state_at(PLAN, Phase.CLOSING)
    result = step(PLAN, state, Command(now_ms=state.clock_ms, command="skip"))
    assert result.directive is None
    assert result.state.phase is Phase.CLOSING


def test_skip_in_question_phase_asks_next_question() -> None:
    result = step(PLAN, deep_state(), Command(now_ms=200_000, command="skip"))
    assert result.state.current_question_id == "q2"
    assert result.state.follow_ups == 0
    assert result.directive is not None
    assert (result.directive.kind, result.directive.reason) == ("ask", "user_skip")


def test_skip_in_question_phase_advances_when_no_question_left() -> None:
    state = deep_state()
    state = state.model_copy(update={"current_question_id": "q2", "asked": ["q1", "q2"]})
    result = step(PLAN, state, Command(now_ms=state.clock_ms, command="skip"))
    assert result.state.phase is Phase.TECHNICAL
    assert result.directive is not None
    assert (result.directive.kind, result.directive.reason) == ("ask", "user_skip")
    assert result.directive.transition_from is Phase.RESUME_DEEP_DIVE


def test_skip_in_non_question_phase_advances() -> None:
    state = state_at(PLAN, Phase.SELF_INTRO)
    result = step(PLAN, state, Command(now_ms=state.clock_ms, command="skip"))
    assert result.state.phase is Phase.RESUME_DEEP_DIVE
    assert result.directive is not None
    assert (result.directive.kind, result.directive.reason) == ("ask", "user_skip")


# ---- Think ----


def test_think_ignored_in_closing() -> None:
    state = state_at(PLAN, Phase.CLOSING)
    result = step(PLAN, state, Think(now_ms=state.clock_ms))
    assert result.state.thinking_since_ms is None
    assert result.directive is None


def test_think_ignored_while_paused() -> None:
    state = deep_state(paused=True)
    result = step(PLAN, state, Think(now_ms=state.clock_ms))
    assert result.state.thinking_since_ms is None
    assert result.directive is None


def test_think_marks_waiting() -> None:
    state = state_at(PLAN, Phase.SELF_INTRO)
    result = step(PLAN, state, Think(now_ms=state.clock_ms))
    assert result.state.thinking_since_ms == result.state.active_ms
    assert result.state.waiting_for_thought is True
    assert result.directive is None


def test_pause_stops_the_clock_and_resume_restarts_it() -> None:
    result = step(PLAN, start(PLAN, now_ms=0).state, Tick(now_ms=1_000))
    paused = step(PLAN, result.state, Command(now_ms=1_000, command="pause"))
    during = step(PLAN, paused.state, Tick(now_ms=1_000_000))
    assert during.state.active_ms == 1_000
    resumed = step(PLAN, during.state, Command(now_ms=1_000_000, command="resume"))
    after = step(PLAN, resumed.state, Tick(now_ms=1_000_001))
    assert after.state.active_ms == 1_001


# ---- 空阶段与整场走完 ----


def test_empty_deep_dive_stage_is_skipped() -> None:
    result = start(EMPTY_DEEP_PLAN, now_ms=0)
    result = step(
        EMPTY_DEEP_PLAN,
        result.state,
        Decision(now_ms=1, action="next_question"),
    )
    assert result.state.phase is Phase.SELF_INTRO
    result = step(
        EMPTY_DEEP_PLAN,
        result.state,
        Decision(now_ms=2, action="next_question"),
    )
    assert result.state.phase is Phase.TECHNICAL
    assert result.directive is not None
    assert result.directive.kind == "ask"
    assert result.directive.transition_from is Phase.SELF_INTRO
    assert result.state.asked == ["q1"]


@pytest.mark.parametrize(
    ("duration_min", "interview_type"),
    [
        (15, InterviewType.TECHNICAL),
        (20, InterviewType.TECHNICAL),
        (30, InterviewType.TECHNICAL),
        (15, InterviewType.BEHAVIORAL),
        (30, InterviewType.BEHAVIORAL),
    ],
)
def test_plan_can_finish(duration_min: int, interview_type: InterviewType) -> None:
    plan = make_plan(duration_min, interview_type)
    result = start(plan, now_ms=0)
    for now in range(1, 101):
        if result.state.phase is Phase.ENDED:
            break
        result = step(plan, result.state, Decision(now_ms=now, action="next_question"))
    assert result.state.phase is Phase.ENDED
    assert result.state.current_question_id is None


# ---- hypothesis ----


def phase_order(plan: InterviewPlan) -> list[Phase]:
    return [*(Phase(slot.stage.value) for slot in plan.stages), Phase.ENDED]


def main_phase(plan: InterviewPlan) -> Phase:
    if any(slot.stage.value == Stage.TECHNICAL.value for slot in plan.stages):
        return Phase.TECHNICAL
    return Phase.BEHAVIORAL


def check_invariants(
    plan: InterviewPlan,
    state: InterviewState,
    follow_up_counts: dict[str, int],
    previous_index: int,
) -> int:
    assert 0 <= state.follow_ups <= MAX_FOLLOW_UPS
    assert len(state.asked) == len(set(state.asked))
    assert set(state.asked) <= {question.id for question in plan.questions}
    index = phase_order(plan).index(state.phase)
    assert index >= previous_index
    if state.phase is Phase.RESUME_DEEP_DIVE or state.phase is main_phase(plan):
        assert state.current_question_id is not None
        question = next(
            question for question in plan.questions if question.id == state.current_question_id
        )
        assert question.stage.value == state.phase.value
    else:
        assert state.current_question_id is None
    assert all(count <= MAX_FOLLOW_UPS for count in follow_up_counts.values())
    return index


@st.composite
def timed_event(draw: Any) -> tuple[int, Tick | Decision | Command | Think]:
    delta = draw(st.integers(min_value=0, max_value=400_000))
    kind = draw(st.integers(min_value=0, max_value=3))
    if kind == 0:
        event: Tick | Decision | Command | Think = Tick(now_ms=0)
    elif kind == 1:
        event = Decision(
            now_ms=0,
            action=draw(st.sampled_from(["follow_up", "next_question", "next_stage", "wrap_up"])),
            focus=draw(st.sampled_from(["", "性能", "协作"])),
        )
    elif kind == 2:
        event = Command(
            now_ms=0,
            command=draw(st.sampled_from(["pause", "resume", "repeat", "skip", "end"])),
        )
    else:
        event = Think(now_ms=0)
    return delta, event


@settings(max_examples=300, deadline=None)
@given(
    plan_index=st.integers(min_value=0, max_value=2),
    sequence=st.lists(timed_event(), max_size=60),
)
def test_random_event_sequences_keep_invariants(
    plan_index: int,
    sequence: list[tuple[int, Tick | Decision | Command | Think]],
) -> None:
    plan = [PLAN, EMPTY_DEEP_PLAN, BEHAVIORAL_PLAN][plan_index]
    state = start(plan, now_ms=0).state
    now = 0
    previous_index = 0
    follow_up_counts: dict[str, int] = {}

    for delta, template in sequence:
        now += delta
        event = template.model_copy(update={"now_ms": now})
        before = state.model_dump_json()
        result = step(plan, state, event)
        assert state.model_dump_json() == before
        if result.directive is not None and result.directive.kind == "follow_up":
            question_id = result.directive.question_id
            assert question_id is not None
            follow_up_counts[question_id] = follow_up_counts.get(question_id, 0) + 1
        previous_index = check_invariants(plan, result.state, follow_up_counts, previous_index)
        loaded = InterviewState.model_validate_json(result.state.model_dump_json())
        assert loaded == result.state
        state = loaded

    now = max(now, state.clock_ms)
    state = step(plan, state, Command(now_ms=now, command="resume")).state
    state = step(plan, state, Tick(now_ms=now + 1_000_000_000)).state
    state = step(plan, state, Tick(now_ms=now + 2_000_000_000)).state
    assert state.phase is Phase.ENDED
