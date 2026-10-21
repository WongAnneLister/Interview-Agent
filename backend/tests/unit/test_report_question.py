"""报告单题复评、证据重试和汇总输出约束。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError
from test_api import exporter as exporter
from test_api import make_settings
from test_evaluator import CANDIDATE_ID, COMPETENCY, INTERVIEW_ID, QUESTION, TURNS, USER

from interview.config import ReportSettings
from interview.db.models import UsageRole
from interview.domain.report_models import QuestionReviewResult, ReportSummaryDraft
from interview.domain.report_question import review_question
from interview.observability import usage
from interview.prompts.loader import DATA_NOTICE, get_prompt_library
from interview.prompts.structured import generate_structured
from interview.providers.base import ProviderTimeout
from interview.providers.fake import FakeLLM
from interview.providers.observed import CallContext
from interview.providers.registry import ProviderFactories, ProviderRegistry


def draft(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "score": 4,
        "evidence": [{"turn_id": str(CANDIDATE_ID), "quote": "本地消息表保证可靠投递"}],
        "summary": "回答说明了可靠投递和幂等处理。",
        "strengths": ["具体实践"],
        "missing_points": ["说明重试后的失败处理"],
        "improved_answer": "我用了本地消息表保证可靠投递，补充具体数据。",
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def scripted(*replies: str) -> FakeLLM:
    queue = iter(replies)
    return FakeLLM(reply=lambda _messages: next(queue), chunk_size=10_000)


async def review(llm: FakeLLM) -> QuestionReviewResult:
    return await review_question(
        llm,
        QUESTION,
        COMPETENCY,
        TURNS,
        interview_id=INTERVIEW_ID,
        settings=ReportSettings(question_timeout_s=17),
    )


async def test_final_review_keeps_feedback_evidence_and_timeout() -> None:
    llm = scripted(draft())
    result = await review(llm)
    assert result.asked and result.question_id == "q1" and result.question == QUESTION.text
    assert result.score == 4 and result.evidence[0].turn_id == CANDIDATE_ID
    assert result.evidence[0].start_ms == 1_000
    assert result.summary and result.strengths and result.missing_points and result.improved_answer
    assert (result.provider, result.model, result.prompt_version) == (
        "fake",
        "fake-llm",
        "report_question@1",
    )
    assert result.latency_ms >= 0 and result.retries == 0
    assert llm.calls[0]["timeout_s"] == 17
    system, user = llm.calls[0]["messages"]
    assert DATA_NOTICE in system["content"]
    assert "不编造" in system["content"] and "不评价口音" in system["content"]
    assert '<user_data type="question">' in user["content"]
    assert '<user_data type="transcript">' in user["content"]
    assert "备选追问方向" not in user["content"]


@pytest.mark.parametrize("second_valid", [True, False])
async def test_forged_evidence_retries_once_and_preserves_feedback(second_valid: bool) -> None:
    bad = draft(evidence=[{"turn_id": str(CANDIDATE_ID), "quote": "不存在的项目成果"}])
    llm = scripted(bad, draft() if second_valid else bad)
    result = await review(llm)
    assert result.score == (4 if second_valid else None)
    assert bool(result.evidence) == second_valid
    assert result.summary and result.improved_answer
    assert result.evidence_retried and result.retries == 1
    assert result.evidence_dropped == (1 if second_valid else 2)
    assert len(llm.calls) == 2
    assert "上一次的引用在候选人原话里找不到" in llm.calls[1]["messages"][1]["content"]


async def test_interviewer_evidence_cannot_receive_score() -> None:
    bad = draft(evidence=[{"turn_id": str(TURNS[0].turn_id), "quote": TURNS[0].text}])
    result = await review(scripted(bad, bad))
    assert result.score is None and result.evidence == [] and result.evidence_dropped == 2


async def test_partial_forgery_drops_only_invalid_quote_without_retry() -> None:
    llm = scripted(
        draft(
            evidence=[
                {"turn_id": str(CANDIDATE_ID), "quote": "本地消息表保证可靠投递"},
                {"turn_id": str(CANDIDATE_ID), "quote": "不存在的项目成果"},
            ]
        )
    )
    result = await review(llm)
    assert result.score == 4 and len(result.evidence) == 1
    assert result.evidence_dropped == 1 and not result.evidence_retried
    assert len(llm.calls) == 1


async def test_null_score_without_evidence_needs_no_retry() -> None:
    llm = scripted(draft(score=None, evidence=[]))
    result = await review(llm)
    assert result.score is None and not result.evidence_retried and len(llm.calls) == 1


async def test_explicit_evidence_and_structured_retries_are_accumulated() -> None:
    bad = draft(evidence=[])
    result = await review(scripted("invalid", bad, "invalid", draft()))
    assert result.retries == 3 and result.score == 4


async def test_report_question_span(exporter: InMemorySpanExporter) -> None:
    await review(scripted(draft()))
    attrs = next(s.attributes for s in exporter.get_finished_spans() if s.name == "report.question")
    assert attrs is not None
    assert attrs["question.id"] == "q1" and attrs["interview.id"] == str(INTERVIEW_ID)
    assert attrs["agent.role"] == "report"
    assert attrs["report.evidence_kept"] == 1 and attrs["report.evidence_dropped"] == 0
    assert attrs["report.score_nulled"] is False and attrs["report.retries"] == 0


async def test_failed_question_span_identifies_question(exporter: InMemorySpanExporter) -> None:
    with pytest.raises(ProviderTimeout):
        await review(FakeLLM(errors=[ProviderTimeout("timeout")]))
    attrs = next(s.attributes for s in exporter.get_finished_spans() if s.name == "report.question")
    assert attrs is not None and attrs["question.id"] == "q1"


async def test_evidence_retry_records_report_usage_and_actual_fallback_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[dict[str, Any]] = []

    async def record(**kwargs: Any) -> None:
        records.append(kwargs)

    monkeypatch.setattr(usage, "record_usage", record)
    config = make_settings().providers
    config.llm_eval.provider, config.llm_eval.model = "primary", "eval-max"
    config.llm_fallback.provider, config.llm_fallback.model = "backup", "backup-flash"
    primary = FakeLLM(errors=[ProviderTimeout("timeout"), ProviderTimeout("timeout")])
    backup = scripted(draft(evidence=[]), draft())
    backup.provider, backup.model = "backup", "backup-flash"
    registry = ProviderRegistry(
        config,
        factories=ProviderFactories(
            llm={
                "primary": lambda _role: primary,
                "backup": lambda _role: backup,
            }
        ),
    )
    try:
        llm = registry.llm(
            "llm_eval",
            CallContext(
                user_id=USER,
                interview_id=INTERVIEW_ID,
                usage_role=UsageRole.REPORT,
            ),
        )
        result = await review_question(
            llm, QUESTION, COMPETENCY, TURNS, interview_id=INTERVIEW_ID, settings=ReportSettings()
        )
    finally:
        await registry.aclose()
    assert (result.provider, result.model) == ("backup", "backup-flash")
    assert [item["is_retry"] for item in records] == [False, True]
    assert all(item["role"] == UsageRole.REPORT for item in records)


def summary(names: list[str], improvements: list[str] | None = None) -> str:
    return json.dumps(
        {
            "competencies": [{"name": name, "conclusion": "结论"} for name in names],
            "overall": "总体总结。",
            "top_improvements": improvements
            if improvements is not None
            else ["建议一", "建议二", "建议三"],
        }
    )


@pytest.mark.parametrize("names", [["A"], ["A", "B", "B"], ["A", "C"]])
def test_summary_requires_exact_competency_names_without_duplicates(names: list[str]) -> None:
    with pytest.raises(ValidationError):
        ReportSummaryDraft.model_validate_json(
            summary(names), context={"expected_names": ["A", "B"]}
        )


def test_summary_requires_three_improvements() -> None:
    with pytest.raises(ValidationError):
        ReportSummaryDraft.model_validate_json(
            summary(["A"], ["one"]), context={"expected_names": ["A"]}
        )


async def test_summary_invalid_names_use_structured_retry() -> None:
    prompt = get_prompt_library().render("report_summary", structured_data='{"competencies": []}')
    llm = scripted(summary(["A"]), summary(["A", "B"]))
    result = await generate_structured(
        llm, prompt, ReportSummaryDraft, context={"expected_names": ["A", "B"]}, timeout_s=10
    )
    assert result.retries == 1 and len(llm.calls) == 2
