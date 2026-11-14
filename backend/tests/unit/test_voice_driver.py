"""语音驱动：假编排器按脚本吐事件，假 Speaker 记录说了什么、怎么说的。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from interview.db.models import Language
from interview.domain.orchestrator_events import (
    EndedEvent,
    InterviewerDelta,
    InterviewerDone,
    InterviewerSentence,
    OrchestratorEvent,
    StateEvent,
)
from interview.domain.state_machine import InterviewState
from interview.voice.driver import DISCLOSURE, RESUME, SessionClock, VoiceDriver

TURN = uuid.uuid4()


def sentence(text: str, *, transition: bool = False) -> InterviewerSentence:
    return InterviewerSentence(text=text, transition=transition)


def speech(*texts: str) -> list[OrchestratorEvent]:
    """面试官说一段话：逐句、增量和结束事件都有。"""
    events: list[OrchestratorEvent] = [StateEvent(value="speaking")]
    for text in texts:
        events += [InterviewerDelta(text=text), sentence(text)]
    return [*events, InterviewerDone(turn_id=TURN, text="".join(texts))]


@dataclass
class FakePlayout:
    name: str
    log: list[str]

    async def wait(self) -> None:
        self.log.append(f"wait:{self.name}")


@dataclass
class FakeSink:
    index: int
    log: list[str]
    sentences: list[str] = field(default_factory=list[str])
    closed: bool = False

    def push(self, sentence: str) -> None:
        assert not self.closed
        self.sentences.append(sentence)

    def close(self) -> FakePlayout:
        self.closed = True
        self.log.append(f"close:{self.index}")
        return FakePlayout(f"stream{self.index}", self.log)


@dataclass
class FakeSpeaker:
    log: list[str] = field(default_factory=list[str])
    said: list[str] = field(default_factory=list[str])
    sinks: list[FakeSink] = field(default_factory=list[FakeSink])

    def say(self, text: str) -> FakePlayout:
        self.said.append(text)
        self.log.append(f"say:{text}")
        return FakePlayout(text, self.log)

    def stream(self) -> FakeSink:
        sink = FakeSink(len(self.sinks), self.log)
        self.sinks.append(sink)
        self.log.append(f"stream:{sink.index}")
        return sink


@dataclass
class FakeOrchestrator:
    state: InterviewState | None = None
    opening: Sequence[OrchestratorEvent] = ()
    replies: list[Sequence[OrchestratorEvent]] = field(default_factory=list[Sequence[Any]])
    on_ticks: dict[int, Sequence[OrchestratorEvent]] = field(default_factory=dict[int, Any])
    repeat: Sequence[OrchestratorEvent] = ()
    calls: list[tuple[Any, ...]] = field(default_factory=list[tuple[Any, ...]])
    checkpoints: int = 0
    closed: bool = False

    async def _emit(self, events: Sequence[OrchestratorEvent]) -> AsyncGenerator[OrchestratorEvent]:
        for event in events:
            yield event

    def start(self, now_ms: int) -> AsyncGenerator[OrchestratorEvent]:
        self.calls.append(("start", now_ms))
        return self._emit(self.opening)

    def on_user_turn(
        self, text: str, start_ms: int, end_ms: int
    ) -> AsyncGenerator[OrchestratorEvent]:
        self.calls.append(("turn", text, start_ms, end_ms))
        return self._emit(self.replies.pop(0) if self.replies else ())

    def on_tick(self, now_ms: int) -> AsyncGenerator[OrchestratorEvent]:
        ticks = sum(1 for call in self.calls if call[0] == "tick")
        self.calls.append(("tick", now_ms))
        return self._emit(self.on_ticks.get(ticks, ()))

    def on_command(self, command: str, now_ms: int) -> AsyncGenerator[OrchestratorEvent]:
        self.calls.append(("command", command, now_ms))
        return self._emit(self.repeat)

    async def checkpoint(self) -> None:
        self.checkpoints += 1

    async def aclose(self) -> None:
        self.closed = True


class FakeWall:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(
    orchestrator: FakeOrchestrator, *, base_ms: int = 0, tick_s: float = 0.01
) -> tuple[VoiceDriver, FakeSpeaker, FakeWall]:
    wall = FakeWall()
    speaker = FakeSpeaker()
    driver = VoiceDriver(
        orchestrator,
        speaker,
        SessionClock(base_ms, wall=wall),
        language=Language.ZH,
        tick_interval_s=tick_s,
    )
    return driver, speaker, wall


def test_clock_counts_from_session_start_and_resumes_from_base() -> None:
    wall = FakeWall()
    clock = SessionClock(5000, wall=wall)
    assert clock.now_ms() == 5000
    wall.now += 1.25
    assert clock.now_ms() == 6250
    assert clock.at(wall.now - 0.5) == 5750
    assert clock.at(999.0) == 5000  # 早于会话开始的事件不会算成负数


async def test_new_interview_discloses_then_streams_opening_and_ends() -> None:
    orchestrator = FakeOrchestrator(opening=[*speech("你好。", "请介绍自己。"), EndedEvent()])
    driver, speaker, _ = make(orchestrator)
    assert await driver.run() is True
    assert speaker.said == [DISCLOSURE[Language.ZH]]
    assert speaker.sinks[0].sentences == ["你好。 ", "请介绍自己。 "]  # 补空格，整句立即放行
    assert speaker.log == [f"say:{DISCLOSURE[Language.ZH]}", "stream:0", "close:0", "wait:stream0"]
    assert orchestrator.calls == [("start", 0)]
    assert (orchestrator.checkpoints, orchestrator.closed) == (1, True)


async def test_transition_is_said_at_once_before_the_reply_stream() -> None:
    reply = [StateEvent(value="thinking"), sentence("嗯，明白了。", transition=True)]
    reply += [*speech("那说说缓存。"), EndedEvent()]
    orchestrator = FakeOrchestrator(opening=speech("请介绍自己。"), replies=[reply])
    driver, speaker, wall = make(orchestrator, tick_s=5)
    task = asyncio.create_task(driver.run())
    await asyncio.sleep(0)
    wall.now += 3.0
    driver.user_turn("我做过支付系统。", start_ms=1200, end_ms=2900)
    assert await task is True
    assert orchestrator.calls == [("start", 0), ("turn", "我做过支付系统。", 1200, 2900)]
    assert speaker.said == [DISCLOSURE[Language.ZH], "嗯，明白了。"]
    assert [s.sentences for s in speaker.sinks] == [["请介绍自己。 "], ["那说说缓存。 "]]
    # 过渡语排在下一段正文之前，收尾等的是最后一段
    assert speaker.log[-4:] == ["say:嗯，明白了。", "stream:1", "close:1", "wait:stream1"]


async def test_turns_are_handled_in_order() -> None:
    orchestrator = FakeOrchestrator(replies=[speech("一。"), [*speech("二。"), EndedEvent()]])
    orchestrator.state = object()  # type: ignore[assignment]  # 已开始：不说声明、不 start
    driver, speaker, _ = make(orchestrator, tick_s=5)
    driver.user_turn("第一轮", 0, 100)
    driver.user_turn("第二轮", 200, 150)  # 结束早于开始时按开始算
    assert await driver.run() is True
    turns = [c[1:] for c in orchestrator.calls if c[0] == "turn"]
    assert turns == [("第一轮", 0, 100), ("第二轮", 200, 200)]
    assert [s.sentences for s in speaker.sinks] == [["一。 "], ["二。 "]]


async def test_resumed_interview_says_continue_and_repeats_the_question() -> None:
    orchestrator = FakeOrchestrator(repeat=speech("请介绍一下你做过的支付系统。"))
    orchestrator.state = object()  # type: ignore[assignment]
    driver, speaker, _ = make(orchestrator, base_ms=90_000, tick_s=5)
    task = asyncio.create_task(driver.run())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert speaker.said == [RESUME[Language.ZH]]  # 不再说声明
    assert orchestrator.calls[0] == ("command", "repeat", 90_000)
    assert speaker.sinks[0].sentences == ["请介绍一下你做过的支付系统。 "]


async def test_ticks_advance_time_and_their_speech_is_spoken() -> None:
    orchestrator = FakeOrchestrator(on_ticks={2: [*speech("时间到了，我们收尾。"), EndedEvent()]})
    orchestrator.state = object()  # type: ignore[assignment]
    driver, speaker, wall = make(orchestrator, base_ms=60_000)
    wall.now += 1.0
    assert await driver.run() is True
    ticks = [c for c in orchestrator.calls if c[0] == "tick"]
    assert len(ticks) == 3
    assert all(now >= 61_000 for _, now in ticks)  # 续面从上次的 clock_ms 接着走
    assert speaker.sinks[0].sentences == ["时间到了，我们收尾。 "]


async def test_cancel_still_checkpoints_and_closes() -> None:
    orchestrator = FakeOrchestrator(opening=speech("请介绍自己。"))
    driver, speaker, _ = make(orchestrator, tick_s=5)
    task = asyncio.create_task(driver.run())
    await asyncio.sleep(0.01)
    task.cancel()  # 候选人离开房间
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (orchestrator.checkpoints, orchestrator.closed) == (1, True)
    assert speaker.sinks[0].closed


async def test_state_is_reported_after_each_event_stream() -> None:
    orchestrator = FakeOrchestrator(
        opening=speech("请介绍自己。"), replies=[[*speech("好。"), EndedEvent()]]
    )
    seen: list[object] = []
    driver = VoiceDriver(
        orchestrator,
        FakeSpeaker(),
        SessionClock(0, wall=FakeWall()),
        language=Language.ZH,
        tick_interval_s=5,
        on_state=seen.append,
    )
    driver.user_turn("我做过支付系统。", 0, 1000)
    assert await driver.run() is True
    assert len(seen) == 2  # 开场一次、候选人这一轮一次
