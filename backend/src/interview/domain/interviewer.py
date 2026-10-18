"""前台面试官：按状态机指令流式生成台词，并按句切分给语音层。"""

from __future__ import annotations

import asyncio
import contextlib
import random
import re
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence

import structlog
from pydantic import BaseModel, ConfigDict, Field

from interview.db.models import InterviewStyle, Language, TurnRole, UsageRole
from interview.domain.state_machine import Directive, Phase, Reason
from interview.observability.tracing import open_span
from interview.prompts.loader import get_prompt_library
from interview.providers.base import ChatMessage, LLMProvider

MAX_HISTORY_TURNS = 12
MAX_SENTENCE_CHARS = 60

_CJK_END = frozenset("。！？；…")
_EN_END = frozenset(".!?;")
_SENTENCE_END = _CJK_END | _EN_END
_RIGHT = frozenset("”’」』）)")
_COMMA = frozenset("，,、")
_ABBREVIATION = re.compile(r"(?:mr|mrs|dr|e\.g|i\.e|vs|etc)\.$", re.IGNORECASE)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HEADING = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]*")
_LIST = re.compile(r"(?m)^[ \t]*(?:[-*•]|\d+[.)])[ \t]+")

_PHASE_LABELS: Mapping[Phase, str] = {
    Phase.OPENING: "开场",
    Phase.SELF_INTRO: "自我介绍",
    Phase.RESUME_DEEP_DIVE: "简历深挖",
    Phase.TECHNICAL: "技术问题",
    Phase.BEHAVIORAL: "行为问题",
    Phase.CANDIDATE_QUESTIONS: "候选人提问",
    Phase.CLOSING: "收尾",
}
_REASON_HINTS: Mapping[str, str] = {
    "stage_timeout": "时间到了，自然地收尾这道题再进入下一部分。",
    "total_timeout": "总时间到了，自然地收尾并准备结束。",
    "user_skip": "候选人想跳过这道题，自然过渡到下一题。",
    "user_end": "候选人想结束面试，自然收尾。",
    "follow_up_limit": "追问次数已到上限，准备换下一题。",
}

log = structlog.get_logger(__name__)


class HistoryTurn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: TurnRole
    text: str


class InterviewerInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    directive: Directive
    style: InterviewStyle
    language: Language
    history: list[HistoryTurn] = Field(default_factory=list[HistoryTurn])
    candidate_name: str = ""


def _with_reason(text: str, reason: Reason | None) -> str:
    hint = _REASON_HINTS.get(reason) if reason is not None else None
    return f"{text}\n提示：{hint}" if hint else text


def _base_task(directive: Directive) -> str:
    phase = directive.phase
    question = directive.question_text or ""
    if directive.kind == "opening":
        return "开场：向候选人打招呼，介绍本场面试的流程，并说明本次面试由 AI 面试官进行。"
    if directive.kind == "stage_prompt":
        label = _PHASE_LABELS.get(phase, phase.value)
        text = f"引导进入新阶段（{label}，{phase.value}）。"
        if directive.transition_from is not None:
            text += f"从 {directive.transition_from.value} 过渡到 {phase.value}。"
        return text
    if directive.kind == "ask":
        text = "提出这道题。"
        if directive.transition_from is not None:
            text += f"先用一句话从 {directive.transition_from.value} 过渡到 {phase.value}。"
        if question:
            text += f"题目：{question}"
        return text
    if directive.kind == "follow_up":
        parts = ["围绕追问方向追问一句，不要把追问方向原样念出来。"]
        if question:
            parts.append(f"参考主问题：{question}")
        if directive.focus:
            parts.append(f"追问方向：{directive.focus}")
        return "\n".join(parts)
    if directive.kind == "converse":
        return f"在当前阶段（{phase.value}）自然接话，例如回应候选人的提问。"
    if directive.kind == "check_in":
        text = "候选人思考超时，温和地询问要不要给一点提示，或者换一道题。"
        if question:
            text += f"当前题目：{question}"
        return text
    if directive.kind == "pause":
        return "确认暂停，告诉候选人随时可以继续。"
    if directive.kind == "resume":
        return "说“我们继续”，并复述当前题目。" + (f"当前题目：{question}" if question else "")
    if directive.kind == "closing":
        return "感谢候选人，说明报告稍后生成，并道别。"
    return "按指令继续。"


def _task_text(directive: Directive) -> str:
    if directive.kind == "repeat":
        base_kind = directive.repeat_of
        if base_kind and base_kind != "repeat":
            base = directive.model_copy(
                update={"kind": base_kind, "repeat_of": None, "reason": None}
            )
            return _with_reason(
                "把上一条内容换个说法再说一遍。" + _task_text(base), directive.reason
            )
        return _with_reason("把上一条内容换个说法再说一遍。", directive.reason)
    return _with_reason(_base_task(directive), directive.reason)


def clean_output(text: str) -> str:
    text = _LINK.sub(r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = _HEADING.sub("", text)
    text = _LIST.sub("", text)
    return text.strip()


class SentenceSplitter:
    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[str]:
        if not chunk:
            return []
        self._buffer += chunk
        return self._drain(flush=False)

    def flush(self) -> list[str]:
        return self._drain(flush=True)

    def _drain(self, *, flush: bool) -> list[str]:
        sentences: list[str] = []
        while self._buffer:
            end = self._find_end(flush=flush)
            if end is not None:
                sentence = self._buffer[:end].strip()
                self._buffer = self._buffer[end:].lstrip()
                if sentence:
                    sentences.append(sentence)
                continue
            if not flush and len(self._buffer) > MAX_SENTENCE_CHARS:
                comma = max(self._buffer.rfind(char) for char in _COMMA)
                if comma > 0:
                    sentence = self._buffer[: comma + 1].strip()
                    self._buffer = self._buffer[comma + 1 :].lstrip()
                    if sentence:
                        sentences.append(sentence)
                    continue
            break
        if flush and self._buffer:
            sentence = self._buffer.strip()
            self._buffer = ""
            if sentence:
                sentences.append(sentence)
        return sentences

    def _find_end(self, *, flush: bool) -> int | None:
        for index, char in enumerate(self._buffer):
            if char not in _SENTENCE_END:
                continue
            if char == "." and not self._period_is_end(index, flush=flush):
                continue
            end = index + 1
            while end < len(self._buffer) and (
                self._buffer[end] in _SENTENCE_END or self._buffer[end] in _RIGHT
            ):
                end += 1
            # 标点在缓冲末尾时，下一段可能还有“！”“””这类要并进同一句的字符，先等
            if end == len(self._buffer) and not flush:
                return None
            return end
        return None

    def _period_is_end(self, index: int, *, flush: bool) -> bool:
        if _ABBREVIATION.search(self._buffer[: index + 1]):
            return False
        rest = self._buffer[index + 1 :].lstrip("".join(_RIGHT))
        return flush if not rest else rest[0].isspace()


TRANSITIONS: Mapping[Language, Mapping[InterviewStyle, tuple[str, ...]]] = {
    Language.ZH: {
        InterviewStyle.GENTLE: (
            "嗯，明白了。",
            "好的。",
            "嗯，好。",
            "了解。",
            "好的，谢谢。",
            "嗯。",
        ),
        InterviewStyle.PRESSURE: ("好。", "嗯。", "明白。", "了解。", "行。", "继续。"),
    },
    Language.EN: {
        InterviewStyle.GENTLE: ("I see.", "Okay.", "Got it.", "Thanks.", "Right.", "Mm-hmm."),
        InterviewStyle.PRESSURE: ("Okay.", "Right.", "Got it.", "Noted.", "Fine.", "Go on."),
    },
}


class TransitionPicker:
    def __init__(
        self,
        language: Language,
        style: InterviewStyle,
        *,
        rng: random.Random | None = None,
        avoid_last: int = 2,
    ) -> None:
        self._options = TRANSITIONS[language][style]
        if avoid_last < 0 or avoid_last >= len(self._options):
            raise ValueError("avoid_last 必须在 0 到可选句数减一之间")
        self._avoid_last = avoid_last
        self._rng = rng or random.Random()  # noqa: S311
        self._recent: list[str] = []

    def pick(self) -> str:
        forbidden: set[str] = set(self._recent[-self._avoid_last :]) if self._avoid_last else set()
        candidates = [item for item in self._options if item not in forbidden]
        choice = self._rng.choice(candidates)
        self._recent.append(choice)
        self._recent = self._recent[-self._avoid_last :] if self._avoid_last else []
        return choice


class Utterance:
    def __init__(
        self,
        llm: LLMProvider,
        messages: Sequence[ChatMessage],
        *,
        interview_id: uuid.UUID,
        kind: str,
        language: Language,
    ) -> None:
        self._llm = llm
        self._messages = list(messages)
        self._interview_id = interview_id
        self._kind = kind
        self._separator = " " if language == Language.EN else ""
        self._parts: list[str] = []
        self._sentences = 0
        self._first_sentence_ms: float | None = None
        self._cancelled = False
        self._done = False
        self._gen: AsyncGenerator[str, None] | None = None
        self._pending: asyncio.Future[str] | None = None

    def __aiter__(self) -> AsyncIterator[str]:
        return self

    async def __anext__(self) -> str:
        if self._done:
            raise StopAsyncIteration
        if self._gen is None:
            self._gen = self._run()
        # 放进任务里等，aclose() 才能从别的协程（如语音层检测到打断）取消它
        self._pending = asyncio.ensure_future(anext(self._gen))
        try:
            return await self._pending
        except StopAsyncIteration:
            self._done = True
            raise
        except asyncio.CancelledError:
            if self._cancelled:
                raise StopAsyncIteration from None
            raise
        finally:
            self._pending = None

    @property
    def text(self) -> str:
        return self._separator.join(self._parts)

    @property
    def first_sentence_ms(self) -> float | None:
        return self._first_sentence_ms

    async def aclose(self) -> None:
        if self._done:
            return
        self._cancelled = True
        pending = self._pending
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        if self._gen is not None:
            await self._gen.aclose()
        self._done = True

    async def _run(self) -> AsyncGenerator[str, None]:
        started = time.perf_counter()
        splitter = SentenceSplitter()
        span = open_span(
            "interviewer.speak",
            interview_id=str(self._interview_id),
            role=UsageRole.FRONT.value,
        )
        current = span.span
        error: BaseException | None = None
        try:
            with span.activate():
                stream = self._llm.stream_chat(
                    self._messages,
                    max_tokens=200,
                    temperature=0.7,
                    extra=None,
                )
            async with stream:
                while True:
                    with span.activate():
                        try:
                            chunk = await anext(stream)
                        except StopAsyncIteration:
                            break
                    for sentence in splitter.feed(chunk):
                        cleaned = clean_output(sentence)
                        if not cleaned:
                            continue
                        self._parts.append(cleaned)
                        self._sentences += 1
                        if self._first_sentence_ms is None:
                            self._first_sentence_ms = (time.perf_counter() - started) * 1000
                        yield cleaned
            for sentence in splitter.flush():
                cleaned = clean_output(sentence)
                if not cleaned:
                    continue
                self._parts.append(cleaned)
                self._sentences += 1
                if self._first_sentence_ms is None:
                    self._first_sentence_ms = (time.perf_counter() - started) * 1000
                yield cleaned
        except (asyncio.CancelledError, GeneratorExit):
            self._cancelled = True
            raise
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._done = True
            latency_ms = int((time.perf_counter() - started) * 1000)
            current.set_attribute("directive.kind", self._kind)
            current.set_attribute("interviewer.sentences", self._sentences)
            if self._first_sentence_ms is not None:
                current.set_attribute("interviewer.first_sentence_ms", self._first_sentence_ms)
            current.set_attribute("interviewer.cancelled", self._cancelled)
            span.end(error)
            log.info(
                "interviewer_spoke",
                kind=self._kind,
                sentences=self._sentences,
                latency_ms=latency_ms,
                first_sentence_ms=self._first_sentence_ms,
                cancelled=self._cancelled,
            )


def _is_opening(directive: Directive) -> bool:
    return directive.kind == "opening" or (
        directive.kind == "repeat" and directive.repeat_of == "opening"
    )


def speak(llm: LLMProvider, data: InterviewerInput, *, interview_id: uuid.UUID) -> Utterance:
    task = _task_text(data.directive)
    if data.candidate_name and _is_opening(data.directive):
        task = f"{task}\n候选人称呼：{data.candidate_name}"
    history = "\n".join(
        f"{'面试官' if turn.role == TurnRole.INTERVIEWER else '候选人'}：{turn.text}"
        for turn in data.history[-MAX_HISTORY_TURNS:]
    )
    prompt = get_prompt_library().render(
        "interviewer",
        task=task,
        history=history,
        style=data.style.value,
        language=data.language.value,
    )
    return Utterance(
        llm,
        prompt.messages,
        interview_id=interview_id,
        kind=data.directive.kind,
        language=data.language,
    )
