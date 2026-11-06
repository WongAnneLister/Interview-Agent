"""评测驱动：虚拟时钟、“我想一下”、轮数上限，以及模拟候选人的提示词。"""

from __future__ import annotations

import random
import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest

from evals.candidate import SimulatedCandidate
from evals.data import load_personas
from evals.driver import MAX_CANDIDATE_TURNS, TURN_LATENCY_MS, Driver, speech_ms
from interview.db.models import Language
from interview.domain.orchestrator import (
    EndedEvent,
    InterviewerDone,
    InterviewOrchestrator,
    OrchestratorEvent,
    StateEvent,
)
from interview.providers.fake import FakeLLM

ZH = Language.ZH


class FakeOrchestrator:
    """开场说一句，每次回答后说一句；回答 answers_to_end 次后结束。"""

    def __init__(self, answers_to_end: int | None = 2) -> None:
        self.answers_to_end = answers_to_end
        self.calls: list[tuple[Any, ...]] = []

    async def _say(self, text: str, *, end: bool = False) -> AsyncGenerator[OrchestratorEvent]:
        yield StateEvent(value="speaking")
        yield InterviewerDone(turn_id=uuid.uuid4(), text=text)
        if end:
            yield EndedEvent()

    async def _quiet(self) -> AsyncGenerator[OrchestratorEvent]:
        return
        yield  # pragma: no cover

    def start(self, now_ms: int) -> AsyncGenerator[OrchestratorEvent]:
        self.calls.append(("start", now_ms))
        return self._say("你好，请先做个自我介绍。")

    def on_tick(self, now_ms: int) -> AsyncGenerator[OrchestratorEvent]:
        self.calls.append(("tick", now_ms))
        return self._quiet()

    def on_think(self, now_ms: int) -> AsyncGenerator[OrchestratorEvent]:
        self.calls.append(("think", now_ms))
        return self._quiet()

    def on_user_turn(
        self, text: str, start_ms: int, end_ms: int
    ) -> AsyncGenerator[OrchestratorEvent]:
        self.calls.append(("user", start_ms, end_ms))
        answers = sum(1 for call in self.calls if call[0] == "user")
        end = self.answers_to_end is not None and answers >= self.answers_to_end
        return self._say("好的，谢谢。" if end else "能具体说说吗？", end=end)


def candidate(persona: str = "average", reply: str = "我负责过订单系统。") -> SimulatedCandidate:
    return SimulatedCandidate(
        llm=FakeLLM(reply),
        persona=load_personas()[persona],
        resume="张三 后端工程师",
        language=ZH,
        rng=random.Random(0),
    )


def driver(fake: FakeOrchestrator, sim: SimulatedCandidate) -> Driver:
    return Driver(cast(InterviewOrchestrator, fake), sim, ZH)


async def test_virtual_clock_advances_by_speech_and_latency() -> None:
    fake = FakeOrchestrator(answers_to_end=2)
    result = await driver(fake, candidate()).run()
    opening = speech_ms("你好，请先做个自我介绍。", ZH)
    answer = speech_ms("我负责过订单系统。", ZH)
    users = [call for call in fake.calls if call[0] == "user"]
    assert users[0] == ("user", opening, opening + answer)
    follow = speech_ms("能具体说说吗？", ZH)
    second_start = opening + answer + TURN_LATENCY_MS + follow
    assert users[1] == ("user", second_start, second_start + answer)
    closing = speech_ms("好的，谢谢。", ZH)
    assert result.clock_ms == second_start + answer + TURN_LATENCY_MS + closing
    assert (result.ended, result.candidate_turns, result.hit_turn_cap) == (True, 2, False)
    assert len(result.interviewer_texts) == 3


async def test_nervous_candidate_thinks_and_pauses() -> None:
    sim = candidate("nervous")
    sim.rng = random.Random(1)
    fake = FakeOrchestrator(answers_to_end=6)
    result = await driver(fake, sim).run()
    thinks = [call for call in fake.calls if call[0] == "think"]
    assert result.think_turns == len(thinks) > 0
    for think in thinks:
        index = fake.calls.index(think)
        tick = fake.calls[index + 1]
        assert tick[0] == "tick" and tick[1] - think[1] >= 8000


async def test_turn_cap_stops_runaway_interview() -> None:
    result = await driver(FakeOrchestrator(answers_to_end=None), candidate()).run()
    assert (result.ended, result.hit_turn_cap) == (False, True)
    assert result.candidate_turns == MAX_CANDIDATE_TURNS


async def test_candidate_prompt_contains_persona_resume_and_history() -> None:
    sim = candidate("strong", reply="  我在拾光电商做订单中心。 ")
    assert await sim.answer("介绍一下你的项目") == "我在拾光电商做订单中心。"
    await sim.answer("具体怎么做的？")
    llm = cast(FakeLLM, sim.llm)
    messages = llm.calls[-1]["messages"]
    system, user = messages[0]["content"], messages[-1]["content"]
    assert "结构清晰" in system and "180–320" in system
    assert "张三 后端工程师" in user
    assert "面试官：介绍一下你的项目" in user and "候选人：我在拾光电商做订单中心。" in user
    assert "具体怎么做的？" in user


def test_describe_points_at_project_frame() -> None:
    from evals.session import describe

    def inner() -> None:
        raise TimeoutError

    with pytest.raises(TimeoutError) as info:
        inner()
    text = describe(info.value)
    assert text.startswith("TimeoutError @ test_evals_driver.py:")
    assert text.endswith(" inner")
