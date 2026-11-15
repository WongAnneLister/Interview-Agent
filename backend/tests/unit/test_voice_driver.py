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


@dataclass(eq=False)
class FakePlayout:
    name: str
    log: list[str]
    text: str = ""
    interrupted: bool = False
    stopped: bool = False  # 被 driver 主动打断（排在后面的那段）
    callbacks: list[Any] = field(default_factory=list[Any])

    async def wait(self) -> None:
        self.log.append(f"wait:{self.name}")

    @property
    def spoken(self) -> str:
        return "" if self.stopped else self.text

    def interrupt(self) -> None:
        self.stopped = True

    def on_done(self, callback: Any) -> None:
        self.callbacks.append(callback)

    def cut(self, spoken: str) -> None:
        """模拟候选人打断：只播出了 spoken。"""
        self.text, self.interrupted = spoken, True
        for callback in self.callbacks:
            callback(self)


@dataclass
class FakeSink:
    index: int
    log: list[str]
    sentences: list[str] = field(default_factory=list[str])
    closed: bool = False
    playout: FakePlayout = field(init=False)

    def __post_init__(self) -> None:
        self.playout = FakePlayout(f"stream{self.index}", self.log)

    def push(self, sentence: str) -> None:
        assert not self.closed
        self.sentences.append(sentence)

    def close(self) -> FakePlayout:
        self.closed = True
        self.log.append(f"close:{self.index}")
        return self.playout


@dataclass
class FakeSpeaker:
    log: list[str] = field(default_factory=list[str])
    said: list[str] = field(default_factory=list[str])
    sinks: list[FakeSink] = field(default_factory=list[FakeSink])
    playouts: list[FakePlayout] = field(default_factory=list[FakePlayout])
    uninterruptible: list[str] = field(default_factory=list[str])

    def say(self, text: str, *, interruptible: bool = True) -> FakePlayout:
        self.said.append(text)
        self.log.append(f"say:{text}")
        if not interruptible:
            self.uninterruptible.append(text)
        playout = FakePlayout(text, self.log, text=text)
        self.playouts.append(playout)
        return playout

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
    truncated: list[str] = field(default_factory=list[str])

    async def _emit(self, events: Sequence[OrchestratorEvent]) -> AsyncGenerator[OrchestratorEvent]:
        for event in events:
            yield event

    async def truncate_interviewer_turn(self, spoken: str) -> None:
        self.truncated.append(spoken)

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


# ---- 打断（PR-31）----


@dataclass
class CuttingOrchestrator(FakeOrchestrator):
    """第一轮回答说到一半被打断：正文第一句播了一半，候选人开口。"""

    speaker: FakeSpeaker | None = None
    cut_transition: bool = False

    def on_user_turn(
        self, text: str, start_ms: int, end_ms: int
    ) -> AsyncGenerator[OrchestratorEvent]:
        self.calls.append(("turn", text, start_ms, end_ms))
        if len([c for c in self.calls if c[0] == "turn"]) > 1:
            return self._emit([*speech("好的，那你说。"), EndedEvent()])
        return self._reply()

    async def _reply(self) -> AsyncGenerator[OrchestratorEvent]:
        assert self.speaker is not None
        yield sentence("嗯，明白了。", transition=True)
        if self.cut_transition:
            self.speaker.playouts[-1].cut("嗯，")
        yield sentence("那我们聊聊缓存。")
        if not self.cut_transition:
            self.speaker.sinks[-1].playout.cut("那我们聊")
        yield sentence("你是怎么设计失效策略的？")
        yield InterviewerDone(
            turn_id=TURN, text="嗯，明白了。那我们聊聊缓存。你是怎么设计失效策略的？"
        )


async def run_cut(*, cut_transition: bool) -> tuple[CuttingOrchestrator, FakeSpeaker]:
    orchestrator = CuttingOrchestrator(cut_transition=cut_transition)
    orchestrator.state = object()  # type: ignore[assignment]
    driver, speaker, _ = make(orchestrator, tick_s=5)
    orchestrator.speaker = speaker
    driver.user_turn("我做过支付系统。", 0, 100)
    driver.user_turn("等一下，我想补充一点", 200, 300)
    assert await driver.run() is True
    return orchestrator, speaker


async def test_interrupted_reply_stops_and_keeps_only_what_was_spoken() -> None:
    orchestrator, speaker = await run_cut(cut_transition=False)
    # 被打断后生成出来的句子不再推给合成
    assert speaker.sinks[0].sentences == ["那我们聊聊缓存。 "]
    # 处理候选人下一轮之前，先把这一轮面试官发言截成实际播出的部分
    assert orchestrator.truncated == ["嗯，明白了。那我们聊"]
    assert [c[0] for c in orchestrator.calls] == ["command", "turn", "turn"]


async def test_interrupting_the_transition_cancels_the_queued_reply() -> None:
    orchestrator, speaker = await run_cut(cut_transition=True)
    # 正文还没开始就被打断：不再开新的一段
    assert speaker.sinks[0].sentences == ["好的，那你说。 "]
    assert orchestrator.truncated == ["嗯，"]


async def test_disclosure_cannot_be_interrupted() -> None:
    orchestrator = FakeOrchestrator(opening=[*speech("请介绍自己。"), EndedEvent()])
    driver, speaker, _ = make(orchestrator, tick_s=5)
    assert await driver.run() is True
    assert speaker.uninterruptible == [DISCLOSURE[Language.ZH]]
    assert orchestrator.truncated == []
