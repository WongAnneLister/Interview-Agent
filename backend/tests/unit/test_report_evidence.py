"""单题伪造证据经过整份报告生成后也不能进入最终分数或持久化证据。"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest
from test_report import ReportHarness
from test_report import harness as harness

from interview.db.models import ReportStatus
from interview.domain import report
from interview.providers.base import ChatMessage


@pytest.mark.parametrize("second_valid", [True, False])
async def test_generated_report_validates_and_persists_evidence_retry(
    harness: ReportHarness, second_valid: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = harness.script.reply

    def reply(messages: Sequence[ChatMessage]) -> str:
        value = original(messages)
        if "题目：q3" in messages[1]["content"] and (
            harness.script.calls["q3"] == 1 or not second_valid
        ):
            data = json.loads(value)
            data["evidence"][0]["quote"] = "候选人没有说过的原话"
            return json.dumps(data, ensure_ascii=False)
        return value

    monkeypatch.setattr(harness.script, "reply", reply)
    await harness.generate()
    row = await harness.row()
    assert row.status == ReportStatus.READY
    body = report.ReportBody.model_validate(row.summary_json)
    question = next(item for item in body.questions if item.question_id == "q3")
    competency = next(item for item in body.competencies if item.name == "数据库")
    assert question.score == (3 if second_valid else None)
    assert competency.score == (3 if second_valid else None)
    assert bool(question.evidence) == bool(competency.evidence) == second_valid
    assert question.summary and question.improved_answer
    assert harness.script.calls["q3"] == 2
    evaluation = next(item for item in await harness.evaluations() if item.question_id == "q3")
    assert evaluation.score == question.score
    assert bool(evaluation.evidence_json) == second_valid
    assert evaluation.detail_json["evidence_retried"] is True
    assert evaluation.detail_json["evidence_dropped"] == (1 if second_valid else 2)
    assert evaluation.detail_json["retries"] == 1
