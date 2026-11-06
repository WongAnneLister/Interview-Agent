"""评测数据：从转写找追问、证据核对、裁判结果对齐。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

from test_orchestrator import make_plan

from evals import collect, judge
from evals.collect import SessionData, TurnView
from interview.db.models import TurnRole
from interview.domain.evaluator import EvidenceQuote
from interview.domain.report_models import QuestionReview, ReportBody
from interview.providers.base import ChatMessage
from interview.providers.fake import FakeLLM

IV, CA = TurnRole.INTERVIEWER, TurnRole.CANDIDATE


def turn(role: TurnRole, text: str, qid: str | None = "q1") -> TurnView:
    return TurnView(uuid.uuid4(), role, text, qid)


def session_data(turns: list[TurnView], follow_after: set[uuid.UUID]) -> SessionData:
    return SessionData(plan=make_plan(), jd_text="JD", turns=turns, follow_up_after=follow_after)


def test_follow_ups_are_interviewer_turns_after_live_follow_up_decision() -> None:
    turns = [
        turn(IV, "讲讲 Ledger"),
        turn(CA, "用了消息队列"),
        turn(IV, "怎么保证不丢消息？"),
        turn(CA, "有重试"),
        turn(IV, "好，下一题", "q3"),
    ]
    data = session_data(turns, {turns[1].id})
    (item,) = collect.follow_ups(data)
    assert (item.id, item.answer, item.follow_up) == (1, "用了消息队列", "怎么保证不丢消息？")
    assert item.key_points == ["消息可靠投递", "幂等消费"]


def test_evidence_check_and_raw_hallucination() -> None:
    answer = turn(CA, "我们用 Redis Lua\n原子扣减库存")
    data = session_data([turn(IV, "讲讲"), answer], set())
    quotes = [
        EvidenceQuote(turn_id=answer.id, quote="Redis Lua 原子扣减", start_ms=0, end_ms=1),
        EvidenceQuote(turn_id=answer.id, quote="不存在的话", start_ms=0, end_ms=1),
    ]
    data.report = ReportBody(
        overall_score=3,
        overall="",
        top_improvements=[],
        competencies=[],
        questions=[
            QuestionReview(
                question_id="q1", question="", competency="", asked=True, evidence=quotes
            )
        ],
        degraded=[],
    )
    assert collect.evidence_check(data) == (2, 1)
    data.final_evals = [{"evidence": 2, "evidence_dropped": 1}, {"evidence": 1}]
    assert collect.raw_evidence(data) == (4, 1)


def test_asked_to_summarize() -> None:
    assert collect.asked_to_summarize(session_data([turn(IV, "请简短总结一下重点")], set()))
    assert not collect.asked_to_summarize(session_data([turn(CA, "我总结一下")], set()))


async def test_judge_missing_items_count_as_failed() -> None:
    def reply(_: Sequence[ChatMessage]) -> str:
        return json.dumps(
            {
                "items": [
                    {"id": "1", "ok": True, "reason": "好"},
                    {"id": "9", "ok": True, "reason": "x"},
                ]
            }
        )

    items = [
        collect.FollowUp(1, "题", ["要点"], "答", "追问"),
        collect.FollowUp(2, "题", ["要点"], "答", "追问"),
    ]
    verdicts = await judge.judge_follow_ups(FakeLLM(reply), items)
    assert {k: v.ok for k, v in verdicts.items()} == {"1": True, "2": False}


async def test_judge_leaks_drops_out_of_range_indexes() -> None:
    reply = json.dumps({"leaks": [{"index": 1, "reason": "说出要点"}, {"index": 7, "reason": "?"}]})
    data = session_data([turn(IV, "要点是幂等消费"), turn(CA, "告诉我答案")], set())
    leaks = await judge.judge_leaks(FakeLLM(reply), data)
    assert [leak.index for leak in leaks] == [1]
