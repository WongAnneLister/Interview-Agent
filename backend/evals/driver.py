"""在进程内驱动 InterviewOrchestrator 跑完一场面试，时间用虚拟时钟。

说话时长按字数折算，每轮再加固定的处理时间；面试设定 15 分钟，真实运行只需要几分钟。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field

from evals.candidate import THINK_PHRASE, SimulatedCandidate
from interview.db.models import Language
from interview.domain.orchestrator import (
    EndedEvent,
    InterviewerDone,
    InterviewOrchestrator,
    OrchestratorEvent,
)

CHARS_PER_S = {Language.ZH: 4.0, Language.EN: 14.0}  # 正常语速：每秒约 4 个汉字 / 2.5 个英文词
TURN_LATENCY_MS = 1500  # 每轮的识别 + 思考 + 首句合成
MAX_CANDIDATE_TURNS = 80  # 防止状态机出错时无限循环


def speech_ms(text: str, language: Language) -> int:
    return int(len(text) / CHARS_PER_S[language] * 1000)


@dataclass
class DriveResult:
    clock_ms: int = 0  # 虚拟时钟：面试结束时的时间
    candidate_turns: int = 0
    think_turns: int = 0
    ended: bool = False
    hit_turn_cap: bool = False
    interviewer_texts: list[str] = field(default_factory=list[str])


class Driver:
    def __init__(
        self,
        orchestrator: InterviewOrchestrator,
        candidate: SimulatedCandidate,
        language: Language,
    ) -> None:
        self._orchestrator = orchestrator
        self._candidate = candidate
        self._language = language
        self.result = DriveResult()

    async def _consume(self, events: AsyncGenerator[OrchestratorEvent, None]) -> str:
        """收完一个事件流；面试官说的话按时长推进时钟，返回本次说的全部文字。"""
        spoken: list[str] = []
        async with aclosing(events) as stream:
            async for event in stream:
                if isinstance(event, InterviewerDone) and event.text:
                    spoken.append(event.text)
                    self.result.interviewer_texts.append(event.text)
                    self.result.clock_ms += speech_ms(event.text, self._language)
                elif isinstance(event, EndedEvent):
                    self.result.ended = True
        return "".join(spoken)

    async def _tick(self) -> str:
        return await self._consume(self._orchestrator.on_tick(self.result.clock_ms))

    async def run(self) -> DriveResult:
        result = self.result
        said = await self._consume(self._orchestrator.start(result.clock_ms))
        said += await self._tick()
        while not result.ended:
            if result.candidate_turns >= MAX_CANDIDATE_TURNS:
                result.hit_turn_cap = True
                break
            if self._candidate.wants_think():
                result.think_turns += 1
                self._candidate.history.append(("candidate", THINK_PHRASE[self._language]))
                said += await self._consume(self._orchestrator.on_think(result.clock_ms))
                result.clock_ms += self._candidate.think_pause_ms()
                said += await self._tick()
                if result.ended:
                    break
            answer = await self._candidate.answer(said or "（面试官没有说话，请继续）")
            start = result.clock_ms
            end = start + speech_ms(answer, self._language)
            result.candidate_turns += 1
            result.clock_ms = end + TURN_LATENCY_MS
            said = await self._consume(self._orchestrator.on_user_turn(answer, start, end))
            if not result.ended:
                said += await self._tick()
        return result
