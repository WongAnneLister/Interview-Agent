"""报告生成：真实短会话、假模型，不依赖 .env 或外部服务。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlite_helpers import create_tables
from structlog.testing import capture_logs
from test_api import make_settings
from test_orchestrator import INTERVIEW_ID, USER, make_plan, seed

from interview.config import ReportSettings
from interview.db.models import (
    Evaluation,
    EvaluationPhase,
    Interview,
    Plan,
    Report,
    ReportStatus,
    Resume,
    Turn,
    TurnRole,
    UsageRole,
    User,
)
from interview.db.session import make_sessionmaker
from interview.domain import report
from interview.domain.orchestrator_store import EvaluationRepository, TurnRepository
from interview.domain.plan import InterviewPlan, PlanCompetency, PlanRepository
from interview.domain.report_store import ReportRepository, ReportStore
from interview.observability import usage
from interview.providers.base import ChatMessage, ProviderTimeout
from interview.providers.fake import FakeLLM
from interview.providers.registry import ProviderFactories, ProviderRegistry

ANSWER = "我用本地消息表保证可靠投递，消费端先检查幂等键。"
IMPROVED = "我负责可靠投递，先写消息表再投递，补充失败重试和具体数据。"


class Script:
    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.failures: dict[str, int] = {}
        self.summary_bad = False
        self.summary_missing = False
        self.active = 0
        self.peak = 0
        self.delay = 0.01
        self.entered = asyncio.Event()
        self.summary_entered = asyncio.Event()
        self.summary_gate: asyncio.Event | None = None
        self.bad_outputs: dict[str, int] = {}

    def reply(self, messages: Sequence[ChatMessage]) -> str:
        content = "\n".join(message["content"] for message in messages)
        if "QuestionReviewDraft" not in messages[0]["content"]:
            self.calls["summary"] += 1
            # 汇总输入不应出现原话、参考说法或证据。
            assert ANSWER not in content and IMPROVED not in content
            assert "turn_id" not in content
            if self.summary_bad:
                return "bad JSON"
            names = ["后端开发", "系统设计", "数据库", "协作", "未覆盖"]
            if self.summary_missing and self.calls["summary"] == 1:
                names.pop()
            return json.dumps(
                {
                    "competencies": [{"name": name, "conclusion": "反馈"} for name in names],
                    "overall": "整体有具体经历。还有提升空间。请练习数据与取舍。",
                    "top_improvements": ["建议1", "建议2", "建议3"],
                },
                ensure_ascii=False,
            )
        match = re.search(r"题目：(q[1-4])", content)
        assert match is not None
        key = match[1]
        self.calls[key] += 1
        if self.calls[key] <= self.bad_outputs.get(key, 0):
            return "bad JSON"
        if self.calls[key] <= self.failures.get(key, 0):
            raise ProviderTimeout("timeout", provider="fake", model="eval")
        turn_ids = re.findall(r"([0-9a-f-]{36}) \| 候选人 \|", content)
        return json.dumps(
            {
                "score": {"q1": 5, "q2": 2, "q3": 3, "q4": 4}[key],
                "evidence": [{"turn_id": turn_ids[-1], "quote": "本地消息表"}],
                "summary": "候选人介绍了消息表和幂等消费。",
                "strengths": ["具体经历"],
                "missing_points": [f"{key}需要补充失败重试"],
                "improved_answer": IMPROVED,
            },
            ensure_ascii=False,
        )


class DelayedLLM(FakeLLM):
    def __init__(self, script: Script) -> None:
        super().__init__(model="eval", reply=script.reply)
        self.script = script

    async def complete(self, messages: Sequence[ChatMessage], **kwargs: Any) -> Any:
        self.script.active += 1
        self.script.peak = max(self.script.peak, self.script.active)
        self.script.entered.set()
        try:
            if "ReportSummaryDraft" in messages[0]["content"]:
                self.script.summary_entered.set()
                if self.script.summary_gate is not None:
                    await self.script.summary_gate.wait()
            await asyncio.sleep(self.script.delay)
            return await super().complete(messages, **kwargs)
        finally:
            self.script.active -= 1


@dataclass
class ReportHarness:
    sm: async_sessionmaker[AsyncSession]
    providers: ProviderRegistry
    script: Script
    plan: InterviewPlan
    settings: ReportSettings = field(default_factory=ReportSettings)
    events: list[Any] = field(default_factory=list)

    async def generate(self) -> None:
        await report.generate_report(
            interview_id=INTERVIEW_ID,
            user_id=USER,
            sessionmaker=self.sm,
            providers=self.providers,
            settings=self.settings,
        )

    async def row(self) -> Report:
        async with self.sm() as session:
            value = await ReportRepository(session, USER).get(INTERVIEW_ID)
            assert value is not None
            return value

    async def evaluations(self) -> list[Evaluation]:
        async with self.sm() as session:
            return list(
                await EvaluationRepository(session, USER).list(
                    Evaluation.interview_id == INTERVIEW_ID,
                    Evaluation.phase == EvaluationPhase.FINAL,
                )
            )


@pytest.fixture
async def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[ReportHarness]:
    # 用文件库：超时取消的查询会让连接作废，内存库会随唯一的连接一起丢失
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'report.db'}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await create_tables(conn, User, Resume, Interview, Plan, Turn, Evaluation, Report)
    sm = make_sessionmaker(engine)
    plan = make_plan()
    for question in plan.questions:
        question.text = question.id
    plan.questions[1].competency = plan.questions[0].competency
    plan.competencies[0].weight = 2.0
    plan.competencies[2].weight = 3.0
    plan.competencies.append(
        PlanCompetency(
            name="未覆盖", description="未覆盖能力", weight=1, probes=["要点"], covered=False
        )
    )
    await seed(sm, plan)
    async with sm() as session:
        turns = TurnRepository(session, USER)
        seq = 0
        for question in plan.questions:
            for role in [TurnRole.INTERVIEWER, TurnRole.CANDIDATE]:
                seq += 1
                turns.add(
                    Turn(
                        interview_id=INTERVIEW_ID,
                        user_id=USER,
                        seq=seq,
                        role=role,
                        text=ANSWER if role == TurnRole.CANDIDATE else "请举具体例子",
                        start_ms=seq * 100,
                        end_ms=seq * 100 + 90,
                        question_id=question.id,
                    )
                )
        turns.add(
            Turn(
                interview_id=INTERVIEW_ID,
                user_id=USER,
                seq=9,
                role=TurnRole.CANDIDATE,
                text=ANSWER,
                start_ms=1000,
                end_ms=1100,
                question_id="q1",
            )
        )
        await session.commit()
    script = Script()
    cfg = make_settings().providers
    cfg.llm_eval.provider = "fake"
    cfg.llm_eval.model = "eval"
    cfg.llm_fallback.provider = ""
    providers = ProviderRegistry(
        cfg, factories=ProviderFactories(llm={"fake": lambda _: DelayedLLM(script)})
    )
    events: list[Any] = []

    async def record(**event: Any) -> None:
        events.append(event)

    monkeypatch.setattr(usage, "record_usage", record)
    try:
        yield ReportHarness(sm, providers, script, plan, events=events)
    finally:
        await providers.aclose()
        await engine.dispose()


async def test_full_generation_averages_final_only_and_replaces_final(
    harness: ReportHarness,
) -> None:
    async with harness.sm() as session:
        EvaluationRepository(session, USER).add(
            Evaluation(
                interview_id=INTERVIEW_ID,
                user_id=USER,
                phase=EvaluationPhase.LIVE,
                question_id="q1",
                competency="后端开发",
                score=1,
                evidence_json=[],
                detail_json={},
                model="live",
                prompt_version="evaluate_answer@1",
            )
        )
        await session.commit()
    with capture_logs() as logs:
        await harness.generate()
    row = await harness.row()
    assert row.status == ReportStatus.READY and row.generated_at is not None
    assert row.model == "fake/eval"
    assert row.prompt_version == "report_question@1+report_summary@1"
    body = report.ReportBody.model_validate(row.summary_json)
    assert body.disclaimer == report.REPORT_DISCLAIMER
    assert len(body.top_improvements) == 3 and not body.degraded
    scores = {item.name: item.score for item in body.competencies}
    assert scores == {"后端开发": 3.5, "系统设计": None, "数据库": 3.0, "协作": 4.0, "未覆盖": None}
    assert body.overall_score == 3.3
    evaluations = await harness.evaluations()
    assert len(evaluations) == 4
    q1 = next(item for item in evaluations if item.question_id == "q1")
    async with harness.sm() as session:
        last = (await TurnRepository(session, USER).list(Turn.seq == 9))[0]
        assert q1.turn_id == last.id
    assert all(item.next_action is None for item in evaluations)
    assert q1.detail_json["improved_answer"] == IMPROVED
    assert harness.events and all(event["role"] == UsageRole.REPORT for event in harness.events)
    serialized = json.dumps(logs, ensure_ascii=False)
    assert ANSWER not in serialized and IMPROVED not in serialized
    assert any(log["event"] == "report_ready" for log in logs)
    await harness.generate()
    assert len(await harness.evaluations()) == 4


async def test_unanswered_questions_skip_llm(harness: ReportHarness) -> None:
    async with harness.sm() as session:
        TurnRepository(session, USER)
        await session.execute(delete(Turn).where(Turn.question_id == "q4"))
        await session.commit()
    await harness.generate()
    body = report.ReportBody.model_validate((await harness.row()).summary_json)
    question = body.questions[-1]
    assert not question.asked and question.score is None and not question.summary
    assert harness.script.calls["q4"] == 0 and len(await harness.evaluations()) == 3


@pytest.mark.parametrize("failures", [1, 2])
async def test_question_retry_and_degradation(harness: ReportHarness, failures: int) -> None:
    harness.script.failures["q1"] = failures
    await harness.generate()
    row = await harness.row()
    assert row.status == ReportStatus.READY
    body = report.ReportBody.model_validate(row.summary_json)
    assert harness.script.calls["q1"] == 2
    assert body.questions[0].score == (5 if failures == 1 else None)
    assert len(body.degraded) == (0 if failures == 1 else 1)
    evaluation = next(item for item in await harness.evaluations() if item.question_id == "q1")
    assert evaluation.detail_json["retries"] == 1
    assert evaluation.score == body.questions[0].score
    if failures == 1:
        assert any(event["is_retry"] for event in harness.events)


async def test_concurrency_limit(harness: ReportHarness) -> None:
    harness.settings.concurrency = 2
    await harness.generate()
    assert harness.script.peak == 2


@pytest.mark.parametrize(
    "problem", ["all_failed", "unconfigured", "timeout", "no_plan", "unexpected"]
)
async def test_failures_are_sanitized(
    harness: ReportHarness, problem: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = "ProviderTimeout"
    if problem == "all_failed":
        harness.script.failures = dict.fromkeys(["q1", "q2", "q3", "q4"], 2)
    elif problem == "unconfigured":
        harness.providers = ProviderRegistry(make_settings().providers)
        expected = "ProviderNotConfigured"
    elif problem == "timeout":
        harness.settings.total_timeout_s = 0.01
        harness.script.delay = 1
        expected = "TimeoutError"
    elif problem == "no_plan":
        async with harness.sm() as session:
            PlanRepository(session, USER)
            await session.execute(delete(Plan))
            await session.commit()
        expected = "InterviewNotFound"
    elif problem == "unexpected":

        async def broken(*_: Any, **__: Any) -> Any:
            raise RuntimeError(ANSWER)

        monkeypatch.setattr(report, "review_question", broken)
        expected = "RuntimeError"
    with capture_logs() as logs:
        await harness.generate()
    row = await harness.row()
    assert row.status == ReportStatus.FAILED
    assert row.summary_json == {"error": expected} and row.generated_at is None
    assert ANSWER not in json.dumps(logs, ensure_ascii=False)
    if problem == "all_failed":
        failed_log = next(item for item in logs if item["event"] == "report_failed")
        assert failed_log["questions"] == failed_log["degraded"] == 4


async def test_summary_failure_degrades_without_failing_report(harness: ReportHarness) -> None:
    harness.script.summary_bad = True
    await harness.generate()
    body = report.ReportBody.model_validate((await harness.row()).summary_json)
    assert harness.script.calls["summary"] == 3
    assert len(body.degraded) == 1
    assert body.top_improvements == [
        "q2需要补充失败重试",
        "q3需要补充失败重试",
        "q4需要补充失败重试",
    ]
    assert body.competencies[0].conclusion == "得分 3.5 分，详见各题反馈"
    assert body.competencies[-1].conclusion == "本场未考察到"
    assert (await harness.row()).model == "fake/eval"


async def test_summary_names_are_validated_and_retried(harness: ReportHarness) -> None:
    harness.script.summary_missing = True
    await harness.generate()
    assert (await harness.row()).status == ReportStatus.READY
    assert harness.script.calls["summary"] == 2


async def test_runner_returns_immediately_deduplicates_and_drains(harness: ReportHarness) -> None:
    runner = report.ReportRunner(harness.sm, harness.providers, harness.settings)
    started = time.monotonic()
    await report.request_report(INTERVIEW_ID, user_id=USER, runner=runner)
    await report.request_report(INTERVIEW_ID, user_id=USER, runner=runner)
    assert time.monotonic() - started < 0.1 and harness.script.calls.total() == 0
    assert runner.is_running(INTERVIEW_ID)
    await runner.aclose()
    assert not runner.is_running(INTERVIEW_ID)
    assert harness.script.calls["summary"] == 1
    assert (await harness.row()).status == ReportStatus.READY
    assert not runner.start(INTERVIEW_ID, USER)  # 关闭后不再接任务


async def test_request_writes_pending_before_returning(harness: ReportHarness) -> None:
    runner = report.ReportRunner(harness.sm, harness.providers, harness.settings)
    await report.request_report(INTERVIEW_ID, user_id=USER, runner=runner)
    # 任务还没跑，但前端收到 ended 立刻查报告时已经能看到 pending
    assert (await harness.row()).status == ReportStatus.PENDING
    await runner.aclose()
    assert (await harness.row()).status == ReportStatus.READY


async def test_request_without_runner_warns() -> None:
    with capture_logs() as logs:
        await report.request_report(INTERVIEW_ID, user_id=USER, runner=None)
    assert logs[-1]["log_level"] == "warning"


async def test_structured_retries_are_counted_within_one_attempt(harness: ReportHarness) -> None:
    harness.script.bad_outputs["q1"] = 2  # generate_structured 内部重试 2 次后成功
    await harness.generate()
    assert harness.script.calls["q1"] == 3
    q1 = next(item for item in await harness.evaluations() if item.question_id == "q1")
    assert q1.score is not None
    assert q1.detail_json["retries"] == 2


async def test_invalid_json_degrades_without_whole_question_retry(harness: ReportHarness) -> None:
    harness.script.bad_outputs["q1"] = 6
    await harness.generate()
    row = await harness.row()
    assert row.status == ReportStatus.READY
    # 格式错误只走 generate_structured 内部的重试（共 3 次），不再整题重来
    assert harness.script.calls["q1"] == 3
    q1 = next(item for item in await harness.evaluations() if item.question_id == "q1")
    assert q1.model == "fake/eval" and q1.score is None
    assert q1.detail_json["retries"] == 2
    assert "q1: 评分失败" in report.ReportBody.model_validate(row.summary_json).degraded


async def test_evaluations_are_saved_before_summary(harness: ReportHarness) -> None:
    harness.script.summary_gate = asyncio.Event()
    task = asyncio.create_task(harness.generate())
    try:
        await asyncio.wait_for(harness.script.summary_entered.wait(), timeout=2)
        assert len(await harness.evaluations()) == 4
        assert (await harness.row()).status == ReportStatus.PENDING
    finally:
        harness.script.summary_gate.set()
        await task


async def test_runner_shutdown_cancels_overdue_tasks(
    harness: ReportHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def hanging(**_: Any) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(report, "generate_report", hanging)
    harness.settings.total_timeout_s = 0.01
    runner = report.ReportRunner(harness.sm, harness.providers, harness.settings)
    runner.start(INTERVIEW_ID, USER)
    await entered.wait()
    await asyncio.wait_for(runner.aclose(), timeout=1)
    assert cancelled.is_set() and not runner.is_running(INTERVIEW_ID)


async def test_database_write_failure_is_persisted_without_dialogue(
    harness: ReportHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    async def broken(*_: Any, **__: Any) -> None:
        raise SQLAlchemyError(ANSWER)

    monkeypatch.setattr(ReportStore, "ready", broken)
    with capture_logs() as logs:
        await harness.generate()
    assert (await harness.row()).summary_json == {"error": "SQLAlchemyError"}
    assert (await harness.row()).status == ReportStatus.FAILED
    assert ANSWER not in json.dumps(logs, ensure_ascii=False)


@pytest.mark.parametrize("no_answers", [True, False])
async def test_no_scored_competencies_have_no_overall_score(
    harness: ReportHarness,
    no_answers: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if no_answers:
        async with harness.sm() as session:
            TurnRepository(session, USER)
            await session.execute(delete(Turn).where(Turn.role == TurnRole.CANDIDATE))
            await session.commit()
    else:
        original = harness.script.reply

        def no_score(messages: Sequence[ChatMessage]) -> str:
            payload = json.loads(original(messages))
            if "score" in payload:
                payload.update(score=None, evidence=[])
            return json.dumps(payload, ensure_ascii=False)

        monkeypatch.setattr(harness.script, "reply", no_score)
    await harness.generate()
    assert (await harness.row()).status == ReportStatus.READY
    body = report.ReportBody.model_validate((await harness.row()).summary_json)
    assert body.overall_score is None
    assert all(c.score is None and c.conclusion == "本场未考察到" for c in body.competencies)
    if no_answers:
        assert harness.script.calls == {"summary": 1}
        assert all(not q.asked for q in body.questions)
