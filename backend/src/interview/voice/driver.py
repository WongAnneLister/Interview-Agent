"""语音会话里驱动面试编排器：转写 → 编排器 → 句子队列（不依赖 LiveKit，可单独测试）。

- 时钟：会话开始时刻为零点；续面时从上次保存的 clock_ms 接着走，断线期间不计入（与文字面试一致）。
  用墙钟换算，是因为 LiveKit 的事件（如候选人开口）带的是墙钟时间。
- 新面试先说一句固定声明，再转发编排器的开场；进行中的面试重新进房时先说“我们继续”，
  再让编排器重复上一句（通常是当前题目），不然候选人进房后一片安静（PR-29 实测）。
  完整的续面（断线期间的上下文、前端提示）见 PR-39。
- 候选人的每一轮放进收件箱，由 run() 串行交给编排器；没有新一轮时每秒 tick 一次，
  推进阶段超时、思考超时。
- 说话：过渡语用 Speaker.say() 立即合成（盖住评估的等待）；面试官正文逐句推进同一个
  SentenceSink（边生成边说），InterviewerDone 时关闭。每句补一个空格再推：合成插件的分句器
  遇到“句末标点在缓冲末尾”会先等下一段，不补空格每句都要等下一句生成出来才开口（C8）。
- 结束：收到 EndedEvent 等最后一段播完，run() 返回 True；无论怎么退出都 checkpoint() + aclose()。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Protocol

import structlog

from interview.db.models import Language
from interview.domain.orchestrator_events import (
    CommandName,
    EndedEvent,
    InterviewerDone,
    InterviewerSentence,
    OrchestratorEvent,
)
from interview.domain.state_machine import InterviewState

log = structlog.get_logger(__name__)

RESUME = {Language.ZH: "好的，我们继续。", Language.EN: "All right, let's continue."}

DISCLOSURE = {
    Language.ZH: "你好，本次面试由 AI 面试官进行，全程录音用于生成面试报告。",
    Language.EN: (
        "Hello. This interview is conducted by an AI interviewer "
        "and is recorded in full to generate your report."
    ),
}


class Playout(Protocol):
    async def wait(self) -> None:
        """等这段话播完（被取消也算结束）。"""
        ...


class SentenceSink(Protocol):
    def push(self, sentence: str) -> None: ...

    def close(self) -> Playout:
        """不再有新句子；返回整段话的播放句柄。"""
        ...


class Speaker(Protocol):
    def say(self, text: str) -> Playout:
        """一整段文字立即合成，排在已有的话后面播放。"""
        ...

    def stream(self) -> SentenceSink:
        """开始一段边生成边说的话。"""
        ...


class Orchestrator(Protocol):
    @property
    def state(self) -> InterviewState | None: ...

    def start(self, now_ms: int) -> AsyncGenerator[OrchestratorEvent, None]: ...

    def on_user_turn(
        self, text: str, start_ms: int, end_ms: int
    ) -> AsyncGenerator[OrchestratorEvent, None]: ...

    def on_tick(self, now_ms: int) -> AsyncGenerator[OrchestratorEvent, None]: ...

    def on_command(
        self, command: CommandName, now_ms: int
    ) -> AsyncGenerator[OrchestratorEvent, None]: ...

    async def checkpoint(self) -> None: ...

    async def aclose(self) -> None: ...


class SessionClock:
    def __init__(self, base_ms: int = 0, *, wall: Callable[[], float] = time.time) -> None:
        self._base_ms = base_ms
        self._wall = wall
        self._t0 = wall()

    def now_ms(self) -> int:
        return self.at(self._wall())

    def at(self, wall_ts: float) -> int:
        """把墙钟时间换算成会话毫秒；早于会话开始的算 0 点（续面时算 base）。"""
        return self._base_ms + max(0, int((wall_ts - self._t0) * 1000))


@dataclass(frozen=True, slots=True)
class _UserTurn:
    text: str
    start_ms: int
    end_ms: int


class VoiceDriver:
    def __init__(
        self,
        orchestrator: Orchestrator,
        speaker: Speaker,
        clock: SessionClock,
        *,
        language: Language,
        tick_interval_s: float = 1.0,
    ) -> None:
        self._orchestrator = orchestrator
        self._speaker = speaker
        self._clock = clock
        self._language = language
        self._tick_interval_s = tick_interval_s
        self._inbox: asyncio.Queue[_UserTurn] = asyncio.Queue()
        self._sink: SentenceSink | None = None
        self._last: Playout | None = None

    def user_turn(self, text: str, start_ms: int, end_ms: int) -> None:
        self._inbox.put_nowait(_UserTurn(text, start_ms, max(start_ms, end_ms)))

    async def run(self) -> bool:
        """跑到面试结束返回 True；被取消时抛 CancelledError。两种情况都会存档并释放编排器。"""
        orchestrator = self._orchestrator
        try:
            if orchestrator.state is None:
                self._last = self._speaker.say(DISCLOSURE[self._language])
                events = orchestrator.start(self._clock.now_ms())
            else:
                self._last = self._speaker.say(RESUME[self._language])
                events = orchestrator.on_command("repeat", self._clock.now_ms())
            if await self._forward(events):
                return await self._finish()
            while True:
                try:
                    turn = await asyncio.wait_for(self._inbox.get(), self._tick_interval_s)
                except TimeoutError:
                    _raise_if_cancelling()
                    events = orchestrator.on_tick(self._clock.now_ms())
                else:
                    events = orchestrator.on_user_turn(turn.text, turn.start_ms, turn.end_ms)
                if await self._forward(events):
                    return await self._finish()
        finally:
            self._close_sink()
            try:
                await orchestrator.checkpoint()
            except Exception as exc:
                log.warning("voice_checkpoint_failed", error=type(exc).__name__)
            await orchestrator.aclose()

    async def _forward(self, events: AsyncGenerator[OrchestratorEvent, None]) -> bool:
        async with aclosing(events) as stream:
            async for event in stream:
                match event:
                    case InterviewerSentence(transition=True):
                        self._close_sink()
                        self._last = self._speaker.say(event.text)
                    case InterviewerSentence():
                        if self._sink is None:
                            self._sink = self._speaker.stream()
                        self._sink.push(event.text + " ")
                    case InterviewerDone():
                        self._close_sink()
                    case EndedEvent():
                        self._close_sink()
                        return True
                    case _:
                        pass
        return False

    def _close_sink(self) -> None:
        if self._sink is not None:
            self._last = self._sink.close()
            self._sink = None

    async def _finish(self) -> bool:
        if self._last is not None:
            await self._last.wait()  # 收尾语播完再挂断
        return True


def _raise_if_cancelling() -> None:
    """取消和超时同时到达时，超时会把取消吞掉（C16），这里补抛。"""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError
