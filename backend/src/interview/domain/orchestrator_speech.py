"""让前台面试官把一条指令说出来：逐句吐事件，前台模型出错时用固定台词兜底。

只负责“说”这一步，不写库：调用方拿到完整文本后自己写 interviewer turn。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Sequence

import structlog

from interview.db.models import InterviewStyle, Language
from interview.domain.interviewer import HistoryTurn, InterviewerInput, speak
from interview.domain.orchestrator_events import InterviewerDelta, InterviewerSentence
from interview.domain.state_machine import Directive
from interview.observability.prometheus import counter
from interview.observability.tracing import OpenSpan
from interview.providers.base import LLMProvider, ProviderError

log = structlog.get_logger(__name__)

_FRONT_FALLBACK = counter("orchestrator_front_fallback", "前台模型失败后使用兜底台词的次数")


def separator_for(language: Language) -> str:
    return " " if language == Language.EN else ""


def fallback_line(directive: Directive, language: Language) -> str:
    """前台模型不可用时的固定台词：一句致歉，加上题目原文（有的话），面试不中断。"""
    question = directive.question_text or ""
    if language == Language.ZH:
        return "不好意思，我们继续。" + question
    return "Sorry, let's continue." + (f" {question}" if question else "")


class Speech:
    """一次“说话”。迭代 events() 拿逐句事件，结束后读 text 得到实际说出的全部文字。"""

    def __init__(
        self,
        llm: LLMProvider,
        directive: Directive,
        *,
        style: InterviewStyle,
        language: Language,
        history: Sequence[HistoryTurn],
        interview_id: uuid.UUID,
        span: OpenSpan,
    ) -> None:
        self._llm = llm
        self._directive = directive
        self._style = style
        self._language = language
        self._history = list(history)
        self._interview_id = interview_id
        self._span = span
        self.text = ""

    async def events(self) -> AsyncGenerator[InterviewerDelta | InterviewerSentence, None]:
        utterance = speak(
            self._llm,
            InterviewerInput(
                directive=self._directive,
                style=self._style,
                language=self._language,
                history=self._history,
            ),
            interview_id=self._interview_id,
        )
        try:
            iterator = utterance.__aiter__()
            while True:
                try:
                    with self._span.activate():
                        sentence = await anext(iterator)
                except StopAsyncIteration:
                    break
                yield InterviewerDelta(text=sentence)
                yield InterviewerSentence(text=sentence)
            self.text = utterance.text
        except ProviderError as exc:
            fallback = fallback_line(self._directive, self._language)
            partial = utterance.text
            self.text = (
                f"{partial}{separator_for(self._language)}{fallback}" if partial else fallback
            )
            yield InterviewerDelta(text=fallback)
            yield InterviewerSentence(text=fallback)
            _FRONT_FALLBACK.inc()
            log.warning(
                "orchestrator_front_fallback",
                interview_id=str(self._interview_id),
                error=type(exc).__name__,
            )
        finally:
            await utterance.aclose()
