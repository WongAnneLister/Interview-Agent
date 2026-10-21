"""真实 llm_eval 为固定四题生成报告；仅由用户手动运行 make test-live。

使用内存 SQLite，并丢弃用量写入，不依赖数据库隧道。打印每题和整份报告耗时，
验证 60 秒目标及所有引用；默认测试集排除此 live 测试。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import JSON, DefaultClause, MetaData, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from test_llm_live import provider_settings

from interview.config import ReportSettings
from interview.db.models import (
    Base,
    Evaluation,
    EvaluationPhase,
    Interview,
    InterviewStatus,
    InterviewStyle,
    InterviewType,
    Language,
    Plan,
    Report,
    ReportStatus,
    Resume,
    Turn,
    TurnRole,
    UsageRole,
    User,
)
from interview.db.scope import bind_user
from interview.db.session import make_sessionmaker
from interview.domain.evaluator import DraftEvidence, TranscriptTurn, validate_evidence
from interview.domain.plan import InterviewPlan, PlanCompetency, Question, Stage, allocate_stages
from interview.domain.report import ReportBody, generate_report
from interview.observability import usage
from interview.providers.observed import CallContext
from interview.providers.registry import ProviderNotConfigured, ProviderRegistry

USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000121")
INTERVIEW_ID = uuid.UUID("00000000-0000-4000-8000-000000000122")
RESUME_ID = uuid.UUID("00000000-0000-4000-8000-000000000123")
FIXED_ANSWERS = [
    (
        "后端开发",
        "你如何保证消息可靠投递和幂等消费？",
        ["本地消息表", "幂等消费"],
        [
            "我在订单项目里将订单和本地消息表放在同一事务提交，后台任务发送消息并记录发送状态。",
            "消费端用订单号和事件类型做唯一键，先检查处理记录，重复消息直接返回成功；失败会重试。",
        ],
    ),
    (
        "系统设计",
        "如果消息队列出现大量积压，你会怎么定位和处理？",
        ["定位消费瓶颈", "扩容或限流"],
        [
            "我先看消费耗时和积压曲线，区分流量突增还是消费变慢。数据库慢就先限流，"
            "再评估扩容消费者；失败消息放进重试队列，避免阻塞主队列。"
        ],
    ),
    (
        "数据库",
        "你如何定位并优化一条慢查询？",
        ["执行计划", "索引验证"],
        [
            "我先从慢查询日志找出高频慢 SQL，查看执行计划和扫描行数，"
            "结合过滤条件建立联合索引，再在测试环境对比执行计划与查询耗时，检查写入开销。"
        ],
    ),
    (
        "协作",
        "你如何与同事一起推动线上故障修复？",
        ["分工沟通", "复盘跟进"],
        [
            "我在订单故障中先和负责人确认影响范围，让同事检查数据库，我负责排查消息链路。"
            "定位后先回滚止损，持续同步进度，恢复后整理时间线并给改进事项指定负责人。"
        ],
    ),
]


def fixed_plan() -> InterviewPlan:
    return InterviewPlan(
        stages=allocate_stages(20, InterviewType.TECHNICAL),
        competencies=[
            PlanCompetency(
                name=name, description=f"{name}能力", weight=1.0, probes=points, covered=True
            )
            for name, _, points, _ in FIXED_ANSWERS
        ],
        questions=[
            Question(
                id=f"q{index}",
                stage=Stage.TECHNICAL,
                competency=name,
                text=text,
                key_points=points,
                follow_ups=["请补充当时的具体操作", "如何确认改进有效"],
            )
            for index, (name, text, points, _) in enumerate(FIXED_ANSWERS, start=1)
        ],
        tech_terms=[],
        prompt_version="plan_questions@1",
    )


@pytest.fixture
async def report_fixture() -> AsyncIterator[
    tuple[async_sessionmaker[AsyncSession], dict[str, list[TranscriptTurn]]]
]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    try:
        # 只修改测试数据库的 metadata 副本，不改变 ORM 映射或全局 DDL 编译器。
        metadata = MetaData()
        for model in (User, Resume, Interview, Plan, Turn, Evaluation, Report):
            Base.metadata.tables[model.__tablename__].to_metadata(metadata)
        for table in metadata.tables.values():
            for column in table.columns:
                if isinstance(column.type, JSONB):
                    column.type = JSON()
                if isinstance(column.server_default, DefaultClause) and "gen_random_uuid" in str(
                    column.server_default.arg
                ):
                    column.server_default = None
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        sessionmaker = make_sessionmaker(engine)
        plan = fixed_plan()
        transcripts: dict[str, list[TranscriptTurn]] = {}
        async with sessionmaker() as session:
            bind_user(session.sync_session, USER_ID)
            session.add(User(id=USER_ID, email="report-live@example.com"))
            session.add(Resume(id=RESUME_ID, user_id=USER_ID, file_key="live-fixture.pdf"))
            session.add(
                Interview(
                    id=INTERVIEW_ID,
                    user_id=USER_ID,
                    resume_id=RESUME_ID,
                    jd_text="后端工程师",
                    type=InterviewType.TECHNICAL,
                    style=InterviewStyle.GENTLE,
                    language=Language.ZH,
                    duration_min=20,
                    status=InterviewStatus.COMPLETED,
                )
            )
            session.add(
                Plan(
                    user_id=USER_ID,
                    interview_id=INTERVIEW_ID,
                    competencies_json=[c.model_dump(mode="json") for c in plan.competencies],
                    questions_json=[q.model_dump(mode="json") for q in plan.questions],
                    stages_json=[s.model_dump(mode="json") for s in plan.stages],
                    tech_terms_json=[],
                    prompt_version=plan.prompt_version,
                )
            )
            seq = 0
            for question, (_, _, _, answers) in zip(plan.questions, FIXED_ANSWERS, strict=True):
                transcripts[question.id] = []
                for index, text in enumerate([question.text, *answers]):
                    seq += 1
                    turn = TranscriptTurn(
                        turn_id=uuid.uuid5(INTERVIEW_ID, str(seq)),
                        role=TurnRole.INTERVIEWER if index == 0 else TurnRole.CANDIDATE,
                        text=text,
                        start_ms=(seq - 1) * 30_000,
                        end_ms=seq * 30_000,
                    )
                    transcripts[question.id].append(turn)
                    session.add(
                        Turn(
                            id=turn.turn_id,
                            user_id=USER_ID,
                            interview_id=INTERVIEW_ID,
                            question_id=question.id,
                            seq=seq,
                            role=turn.role,
                            text=turn.text,
                            start_ms=turn.start_ms,
                            end_ms=turn.end_ms,
                        )
                    )
            await session.commit()
        yield sessionmaker, transcripts
    finally:
        await engine.dispose()


@pytest.mark.live
async def test_report_fixed_interview(
    report_fixture: tuple[async_sessionmaker[AsyncSession], dict[str, list[TranscriptTurn]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = provider_settings()
    if not settings.llm_eval.configured:
        pytest.skip("LLM_EVAL_PROVIDER / LLM_EVAL_MODEL 未配置")

    async def discard_usage(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(usage, "record_usage", discard_usage)
    sessionmaker, transcripts = report_fixture
    registry = ProviderRegistry(settings)
    try:
        try:
            registry.llm(
                "llm_eval",
                CallContext(
                    user_id=USER_ID, interview_id=INTERVIEW_ID, usage_role=UsageRole.REPORT
                ),
            )
        except ProviderNotConfigured as exc:
            pytest.skip(str(exc))
        started = time.perf_counter()
        await generate_report(
            interview_id=INTERVIEW_ID,
            user_id=USER_ID,
            sessionmaker=sessionmaker,
            providers=registry,
            settings=ReportSettings(),
        )
        elapsed = time.perf_counter() - started
        async with sessionmaker() as session:
            bind_user(session.sync_session, USER_ID)
            report = await session.get(Report, INTERVIEW_ID)
            evaluations = list(
                await session.scalars(
                    select(Evaluation)
                    .where(
                        Evaluation.interview_id == INTERVIEW_ID,
                        Evaluation.user_id == USER_ID,
                        Evaluation.phase == EvaluationPhase.FINAL,
                    )
                    .order_by(Evaluation.question_id)
                )
            )
        with capsys.disabled():
            print(f"\n[report] {settings.llm_eval.provider}/{settings.llm_eval.model}")
            for evaluation in evaluations:
                print(
                    f"  {evaluation.question_id}: {evaluation.detail_json['latency_ms']} ms, "
                    f"score={evaluation.score}, model={evaluation.model}"
                )
            print(f"  total={elapsed:.3f} s")
        assert report is not None and report.status == ReportStatus.READY
        assert elapsed < 60
        body = ReportBody.model_validate(report.summary_json)
        assert len(evaluations) == len(body.questions) == len(FIXED_ANSWERS)
        assert any(question.score is not None for question in body.questions)
        for question in body.questions:
            kept, dropped = validate_evidence(
                [DraftEvidence(turn_id=q.turn_id, quote=q.quote) for q in question.evidence],
                transcripts[question.question_id],
            )
            assert dropped == 0 and len(kept) == len(question.evidence)
            assert question.score is None or kept
        for evaluation in evaluations:
            evidence = [DraftEvidence.model_validate(item) for item in evaluation.evidence_json]
            kept, dropped = validate_evidence(evidence, transcripts[evaluation.question_id])
            assert dropped == 0 and len(kept) == len(evidence)
            assert evaluation.score is None or kept
        turns = [turn for question_turns in transcripts.values() for turn in question_turns]
        for competency in body.competencies:
            kept, dropped = validate_evidence(
                [DraftEvidence(turn_id=q.turn_id, quote=q.quote) for q in competency.evidence],
                turns,
            )
            assert dropped == 0 and len(kept) == len(competency.evidence)
            assert competency.score is None or kept
    finally:
        await registry.aclose()
