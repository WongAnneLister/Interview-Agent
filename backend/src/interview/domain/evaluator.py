"""实时评估：模型打分与原地引用校验，决定追问或换题；不做编排和入库。

传入 on_decision 时流式读取模型输出：JSON 前两个字段 next_action、follow_up_focus 一到齐就回调，
编排器据此决策，不必等分数、证据写完（challenges C19：真实回答下完整输出要 3–5 秒）。
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Self

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

from interview.db.models import NextAction, TurnRole, UsageRole
from interview.domain.plan import PlanCompetency, Question
from interview.domain.state_machine import DEFAULT
from interview.observability.prometheus import counter
from interview.observability.tracing import span
from interview.prompts.loader import RenderedPrompt, get_prompt_library
from interview.prompts.structured import StructuredResult, generate_structured
from interview.providers.base import LLMProvider
from interview.providers.observed import marked_retry

PROMPT_ID = "evaluate_answer"
MIN_QUOTE_CHARS = 4

log = structlog.get_logger(__name__)

_EVIDENCE_COUNTER = counter("evaluator_evidence", "评估器证据保留或丢弃的数量", ["outcome"])
_SCORE_NULLED_COUNTER = counter("evaluator_score_nulled", "因没有有效证据而把分数置空的次数")
_OVERRIDE_COUNTER = counter(
    "evaluator_action_overridden", "follow_up 被代码改成 next_question 的次数", ["reason"]
)

OverrideReason = Literal["follow_up_limit", "focus_leak"]


class TranscriptTurn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    turn_id: uuid.UUID
    role: TurnRole
    text: str
    start_ms: int
    end_ms: int


class EvaluationInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: Question
    competency: PlanCompetency
    turns: list[TranscriptTurn]
    follow_ups: int = Field(ge=0)
    max_follow_ups: int = Field(default=DEFAULT.max_follow_ups, ge=0)
    stage_remaining_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_turns(self) -> Self:
        if not any(turn.role == TurnRole.CANDIDATE for turn in self.turns):
            raise ValueError("本题至少需要一条候选人发言")
        turn_ids = [turn.turn_id for turn in self.turns]
        if len(turn_ids) != len(set(turn_ids)):
            raise ValueError("turn_id 不能重复")
        return self


class DraftEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    turn_id: uuid.UUID
    quote: str = Field(min_length=1)


class EvaluationDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    next_action: NextAction
    follow_up_focus: str = ""
    score: int | None = Field(ge=1, le=5)
    evidence: list[DraftEvidence]
    missing_points: list[str]
    highlights: list[str]
    rationale: str

    @model_validator(mode="after")
    def validate_focus(self) -> Self:
        if self.next_action == NextAction.FOLLOW_UP and not normalize_quote(self.follow_up_focus):
            raise ValueError("追问方向不能为空")
        return self


def focus_leaks_key_point(focus: str, key_points: Sequence[str]) -> bool:
    """追问方向里出现了要点原文（归一化后 ≥ MIN_QUOTE_CHARS 的子串），等于把答案告诉候选人。"""
    normalized_focus = normalize_quote(focus)
    return any(
        len(point) >= MIN_QUOTE_CHARS and point in normalized_focus
        for point in map(normalize_quote, key_points)
    )


class EvidenceQuote(BaseModel):
    turn_id: uuid.UUID
    quote: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class EarlyDecision:
    """从流式输出里提前读到的决策，已按与完整结果相同的规则改写（追问上限、泄露要点）。"""

    action: NextAction
    focus: str
    override_reason: OverrideReason | None
    decision_ms: int


_ACTION_FIELD = re.compile(r'"next_action"\s*:\s*"([a-z_]+)"')
_FOCUS_FIELD = re.compile(r'"follow_up_focus"\s*:\s*"((?:[^"\\]|\\.)*)"')


def extract_decision(text: str) -> tuple[NextAction, str] | None:
    """从不完整的 JSON 里取 next_action 和 follow_up_focus；还不够做决策时返回 None。

    追问需要等追问方向的字符串闭合；换题、换阶段不需要追问方向。
    """
    action_match = _ACTION_FIELD.search(text)
    if action_match is None:
        return None
    try:
        action = NextAction(action_match[1])
    except ValueError:
        return None
    if action != NextAction.FOLLOW_UP:
        return action, ""
    focus_match = _FOCUS_FIELD.search(text)
    if focus_match is None:
        return None
    try:
        focus = str(json.loads(f'"{focus_match[1]}"'))
    except json.JSONDecodeError:
        return None
    return (action, focus) if normalize_quote(focus) else None


def apply_overrides(
    action: NextAction, focus: str, data: EvaluationInput
) -> tuple[NextAction, str, OverrideReason | None]:
    """追问已达上限或追问方向含要点原文时改成换题（不让模型重试，C1 实测重试多 1.5 秒）。"""
    reason: OverrideReason | None = None
    if action == NextAction.FOLLOW_UP:
        if data.follow_ups >= data.max_follow_ups:
            reason = "follow_up_limit"
        elif focus_leaks_key_point(focus, data.question.key_points):
            reason = "focus_leak"
    if reason is not None:
        action = NextAction.NEXT_QUESTION
    return action, (focus if action == NextAction.FOLLOW_UP else ""), reason


class EvaluationResult(BaseModel):
    question_id: str
    competency: str
    next_action: NextAction
    follow_up_focus: str
    score: int | None
    evidence: list[EvidenceQuote]
    missing_points: list[str]
    highlights: list[str]
    rationale: str
    evidence_dropped: int
    evidence_retried: bool
    action_overridden: bool
    override_reason: OverrideReason | None  # 为什么改成了换题
    provider: str  # 实际作答的厂商和型号，切到备用时是备用的
    model: str
    prompt_version: str
    latency_ms: int
    retries: int
    decision_ms: int | None = None  # 流式提前决策时，从开始到拿到决策的耗时


def normalize_quote(text: str) -> str:
    """NFKC、casefold 后去掉空白和 Unicode 标点；符号（如 C++ 的 +）保留。"""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        char
        for char in normalized
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def validate_evidence(
    evidence: Sequence[DraftEvidence], turns: Sequence[TranscriptTurn]
) -> tuple[list[EvidenceQuote], int]:
    """只保留能逐字定位到候选人发言的证据，返回（有效证据, 丢弃数）。"""
    by_id = {turn.turn_id: turn for turn in turns}
    normalized_texts = {turn.turn_id: normalize_quote(turn.text) for turn in turns}
    seen: set[tuple[uuid.UUID, str]] = set()
    kept: list[EvidenceQuote] = []
    dropped = 0
    for item in evidence:
        turn = by_id.get(item.turn_id)
        quote = normalize_quote(item.quote)
        key = (item.turn_id, quote)
        if (
            turn is None
            or turn.role != TurnRole.CANDIDATE
            or len(quote) < MIN_QUOTE_CHARS
            or quote not in normalized_texts[turn.turn_id]
            or key in seen
        ):
            dropped += 1
            continue
        seen.add(key)
        kept.append(
            EvidenceQuote(
                turn_id=turn.turn_id,
                quote=item.quote,
                start_ms=turn.start_ms,
                end_ms=turn.end_ms,
            )
        )
    return kept, dropped


def _question_data(data: EvaluationInput) -> str:
    lines = [
        f"题目：{data.question.text}",
        f"能力项：{data.competency.name}",
        f"能力项描述：{data.competency.description}",
        "好回答应包含的要点：",
        *(f"- {point}" for point in data.question.key_points),
        "备选追问方向：",
        *(f"- {item}" for item in data.question.follow_ups),
    ]
    return "\n".join(lines)


def _transcript(turns: Sequence[TranscriptTurn]) -> str:
    labels = {TurnRole.INTERVIEWER: "面试官", TurnRole.CANDIDATE: "候选人"}
    return "\n".join(f"{turn.turn_id} | {labels[turn.role]} | {turn.text}" for turn in turns)


def _render(data: EvaluationInput, *, evidence_feedback: bool) -> RenderedPrompt:
    return get_prompt_library().render(
        PROMPT_ID,
        question_data=_question_data(data),
        follow_ups=data.follow_ups,
        max_follow_ups=data.max_follow_ups,
        stage_remaining_s=data.stage_remaining_ms // 1000,
        transcript=_transcript(data.turns),
        evidence_feedback=evidence_feedback,
    )


def _count_evidence(kept: int, dropped: int) -> None:
    _EVIDENCE_COUNTER.labels(outcome="kept").inc(kept)
    _EVIDENCE_COUNTER.labels(outcome="dropped").inc(dropped)


async def _evaluate_draft(
    llm: LLMProvider,
    data: EvaluationInput,
    *,
    evidence_feedback: bool,
    on_text: Callable[[str], None] | None = None,
) -> StructuredResult[EvaluationDraft]:
    return await generate_structured(
        llm,
        _render(data, evidence_feedback=evidence_feedback),
        EvaluationDraft,
        max_tokens=2048,
        on_text=on_text,
    )


async def evaluate(
    llm: LLMProvider,
    data: EvaluationInput,
    *,
    interview_id: uuid.UUID,
    on_decision: Callable[[EarlyDecision], None] | None = None,
) -> EvaluationResult:
    """on_decision 不为空时流式调用，决策字段一到齐就回调一次；最终结果沿用这次决策。"""
    started = time.perf_counter()
    early: EarlyDecision | None = None

    def watch(text: str) -> None:
        nonlocal early
        if early is not None or on_decision is None:
            return
        found = extract_decision(text)
        if found is None:
            return
        action, focus, reason = apply_overrides(*found, data)
        early = EarlyDecision(action, focus, reason, int((time.perf_counter() - started) * 1000))
        on_decision(early)

    evidence_dropped = 0
    evidence_retried = False
    retries = 0
    with span(
        "evaluator.evaluate",
        interview_id=str(interview_id),
        role=UsageRole.EVAL.value,
    ) as current:
        first = await _evaluate_draft(
            llm, data, evidence_feedback=False, on_text=watch if on_decision else None
        )
        retries += first.retries
        kept, dropped = validate_evidence(first.value.evidence, data.turns)
        _count_evidence(len(kept), dropped)
        evidence_dropped += dropped
        used = first
        draft = first.value

        if draft.score is not None and not kept:
            evidence_retried = True
            with marked_retry():
                second = await _evaluate_draft(llm, data, evidence_feedback=True)
            retries += second.retries
            kept, dropped = validate_evidence(second.value.evidence, data.turns)
            _count_evidence(len(kept), dropped)
            evidence_dropped += dropped
            used = second
            draft = second.value

        score_nulled = not kept and (draft.score is not None or evidence_retried)
        if score_nulled:
            _SCORE_NULLED_COUNTER.inc()
        if early is not None:
            # 编排器已按提前读到的决策行动，结果里记录同一个决策，保持一致
            action, focus, override_reason = early.action, early.focus, early.override_reason
        else:
            action, focus, override_reason = apply_overrides(
                draft.next_action, draft.follow_up_focus, data
            )
        action_overridden = override_reason is not None
        if override_reason is not None:
            _OVERRIDE_COUNTER.labels(reason=override_reason).inc()
        latency_ms = int((time.perf_counter() - started) * 1000)
        current.set_attributes(
            {
                "question.id": data.question.id,
                "evaluator.evidence_kept": len(kept),
                "evaluator.evidence_dropped": evidence_dropped,
                "evaluator.evidence_retried": evidence_retried,
                "evaluator.score_nulled": score_nulled,
                "evaluator.action_overridden": action_overridden,
                "evaluator.override_reason": override_reason or "",
                "evaluator.latency_ms": latency_ms,
                "evaluator.decision_ms": early.decision_ms if early else -1,
            }
        )
        result = EvaluationResult(
            question_id=data.question.id,
            competency=data.competency.name,
            next_action=action,
            follow_up_focus=focus,
            score=None if score_nulled else draft.score,
            evidence=[] if score_nulled else kept,
            missing_points=draft.missing_points,
            highlights=draft.highlights,
            rationale=draft.rationale,
            evidence_dropped=evidence_dropped,
            evidence_retried=evidence_retried,
            action_overridden=action_overridden,
            override_reason=override_reason,
            provider=used.provider,
            model=used.model,
            prompt_version=f"{used.prompt_id}@{used.prompt_version}",
            latency_ms=latency_ms,
            retries=retries,
            decision_ms=early.decision_ms if early else None,
        )
    log.info(
        "evaluator_evaluated",
        interview_id=str(interview_id),
        question_id=data.question.id,
        evidence_kept=len(result.evidence),
        evidence_dropped=result.evidence_dropped,
        evidence_retried=result.evidence_retried,
        score_nulled=score_nulled,
        action_overridden=result.action_overridden,
        override_reason=result.override_reason,
        retries=result.retries,
        latency_ms=result.latency_ms,
        decision_ms=result.decision_ms,
    )
    return result
