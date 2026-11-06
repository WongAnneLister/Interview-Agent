"""大模型裁判：追问合理性、题目相关性、对抗场次的泄露检查。每场每类各一次调用。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evals.collect import FollowUp, SessionData
from interview.db.models import TurnRole
from interview.prompts.loader import get_prompt_library
from interview.prompts.structured import generate_structured
from interview.providers.base import LLMProvider

JUDGE_MAX_TOKENS = 4096


class Verdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    ok: bool = Field(description="合理 / 相关为 true")
    reason: str


class Verdicts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[Verdict]


class Leak(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int
    reason: str


class LeakReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    leaks: list[Leak]


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)


def _by_id(verdicts: Verdicts, ids: list[str]) -> dict[str, Verdict]:
    """只认输入里有的 id；裁判漏判的条目记为不合格，避免漏判把指标抬高。"""
    found = {v.id: v for v in verdicts.items if v.id in ids}
    return {i: found.get(i) or Verdict(id=i, ok=False, reason="裁判未给出结论") for i in ids}


async def judge_follow_ups(llm: LLMProvider, items: list[FollowUp]) -> dict[str, Verdict]:
    if not items:
        return {}
    payload = [
        {
            "id": str(item.id),
            "题目": item.question,
            "好回答应包含的要点": item.key_points,
            "候选人刚才的回答": item.answer,
            "面试官的追问": item.follow_up,
        }
        for item in items
    ]
    prompt = get_prompt_library().render("eval_judge_followup", items=_dump(payload))
    result = await generate_structured(llm, prompt, Verdicts, max_tokens=JUDGE_MAX_TOKENS)
    return _by_id(result.value, [str(item.id) for item in items])


async def judge_relevance(llm: LLMProvider, data: SessionData) -> dict[str, Verdict]:
    competencies = {c.name: c for c in data.plan.competencies}
    payload = [
        {
            "id": q.id,
            "题目": q.text,
            "能力项": q.competency,
            "能力项描述": competencies[q.competency].description
            if q.competency in competencies
            else "",
        }
        for q in data.plan.questions
    ]
    prompt = get_prompt_library().render(
        "eval_judge_relevance", jd=data.jd_text, items=_dump(payload)
    )
    result = await generate_structured(llm, prompt, Verdicts, max_tokens=JUDGE_MAX_TOKENS)
    return _by_id(result.value, [q.id for q in data.plan.questions])


async def judge_leaks(llm: LLMProvider, data: SessionData) -> list[Leak]:
    utterances = [t.text for t in data.turns if t.role is TurnRole.INTERVIEWER]
    secrets = [{"题目": q.text, "要点": q.key_points} for q in data.plan.questions]
    prompt = get_prompt_library().render(
        "eval_judge_leak",
        secrets=_dump(secrets),
        utterances="\n".join(f"{i}. {text}" for i, text in enumerate(utterances, start=1)),
    )
    result = await generate_structured(llm, prompt, LeakReport, max_tokens=JUDGE_MAX_TOKENS)
    return [leak for leak in result.value.leaks if 1 <= leak.index <= len(utterances)]
