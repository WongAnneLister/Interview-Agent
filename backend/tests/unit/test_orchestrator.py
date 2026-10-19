"""文字面试编排：完整流程、降级、预算、状态持久化和串行。"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool
from sqlite_helpers import create_tables
from structlog.testing import capture_logs
from test_api import exporter as exporter
from test_api import make_settings

from interview.db.models import (
    Evaluation,
    EvaluationPhase,
    Interview,
    InterviewStatus,
    InterviewStyle,
    InterviewType,
    Language,
    NextAction,
    Plan,
    Resume,
    Turn,
    TurnRole,
    User,
)
from interview.db.session import make_sessionmaker
from interview.domain.orchestrator import (
    DEFAULT_CONFIG,
    EndedEvent,
    InterviewerDone,
    InterviewerSentence,
    InterviewOrchestrator,
    OrchestratorConfig,
    OrchestratorEvent,
    StageEvent,
    StateEvent,
)
from interview.domain.orchestrator_store import EvaluationRepository, TurnRepository
from interview.domain.plan import (
    InterviewPlan,
    InterviewRepository,
    PlanCompetency,
    PlanRepository,
    Question,
    Stage,
    allocate_stages,
)
from interview.domain.resume import ResumeRepository
from interview.domain.state_machine import InterviewState, Phase
from interview.observability import budget, usage
from interview.observability.budget import BudgetCheck, BudgetStatus, BudgetUsage
from interview.providers.base import (
    ChatMessage,
    LLMProvider,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)
from interview.providers.fake import FakeLLM
from interview.providers.registry import ProviderFactories, ProviderRegistry

USER = uuid.UUID("00000000-0000-4000-8000-000000000026")
INTERVIEW_ID = uuid.UUID("00000000-0000-4000-8000-000000000027")
RESUME_ID = uuid.UUID("00000000-0000-4000-8000-000000000028")
CANDIDATE_TEXT = "我用了本地消息表保证可靠投递，消费端做幂等处理。"
FRONT_REPLY = "好的。"


def make_plan() -> InterviewPlan:
    stages = allocate_stages(20, InterviewType.TECHNICAL)
    questions = [
        Question(
            id="q1",
            stage=Stage.RESUME_DEEP_DIVE,
            competency="后端开发",
            project="Ledger",
            text="深挖 Ledger 项目",
            key_points=["消息可靠投递", "幂等消费"],
            follow_ups=["追问一", "追问二"],
        ),
        Question(
            id="q2",
            stage=Stage.RESUME_DEEP_DIVE,
            competency="系统设计",
            project="Northwind Labs",
            text="深挖 Northwind Labs 项目",
            key_points=["幂等消费", "失败重试"],
            follow_ups=["追问一", "追问二"],
        ),
        Question(
            id="q3",
            stage=Stage.TECHNICAL,
            competency="数据库",
            project="",
            text="数据库题",
            key_points=["索引", "事务"],
            follow_ups=["追问一", "追问二"],
        ),
        Question(
            id="q4",
            stage=Stage.TECHNICAL,
            competency="协作",
            project="",
            text="协作题",
            key_points=["沟通", "推动"],
            follow_ups=["追问一", "追问二"],
        ),
    ]
    competencies = [
        PlanCompetency(
            name=name,
            description=f"{name}能力",
            weight=1.0,
            probes=["要点"],
            covered=True,
        )
        for name in ("后端开发", "系统设计", "数据库", "协作")
    ]
    return InterviewPlan(
        stages=stages,
        competencies=competencies,
        questions=questions,
        tech_terms=[],
        prompt_version="plan_questions@1",
    )


def budget_result(status: BudgetStatus) -> BudgetCheck:
    usage_value = BudgetUsage(
        spent_cny=Decimal("0"),
        limit_cny=Decimal("1"),
        ratio=Decimal("0"),
        status=status,
    )
    return BudgetCheck(interview=usage_value, daily=usage_value, unpriced_events=0)


@dataclass
class BudgetState:
    status: BudgetStatus = BudgetStatus.OK


class EvalScript:
    def __init__(
        self, actions: Sequence[str] | None = None, default: str = "next_question"
    ) -> None:
        self.actions = list(actions or [])
        self.default = default
        self.calls = 0

    def reply(self, messages: Sequence[ChatMessage]) -> str:
        self.calls += 1
        content = "\n".join(message["content"] for message in messages)
        ids = re.findall(r"([0-9a-f-]{36}) \| 候选人 \|", content)
        turn_id = ids[-1] if ids else str(uuid.uuid4())
        action = self.actions.pop(0) if self.actions else self.default
        focus = "请举一个具体例子" if action == "follow_up" else ""
        payload = {
            "next_action": action,
            "follow_up_focus": focus,
            "score": 4,
            "evidence": [{"turn_id": turn_id, "quote": "本地消息表"}],
            "missing_points": ["失败重试"],
            "highlights": ["幂等"],
            "rationale": "回答具体，覆盖了主要要点。",
        }
        return json.dumps(payload, ensure_ascii=False)


@dataclass
class Harness:
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    orchestrator: InterviewOrchestrator
    front: FakeLLM
    eval_llm: FakeLLM
    script: EvalScript
    budget: BudgetState
    ended: list[uuid.UUID] = field(default_factory=list[uuid.UUID])
    providers: ProviderRegistry | None = None
    plan: InterviewPlan | None = None


async def seed(sessionmaker: async_sessionmaker[AsyncSession], plan: InterviewPlan) -> None:
    async with sessionmaker() as session:
        session.add(User(id=USER, email="local@example.com"))
        ResumeRepository(session, USER).add(
            Resume(
                id=RESUME_ID,
                user_id=USER,
                file_key=f"{USER}/{RESUME_ID}.pdf",
                parsed_json={"name": "Alex"},
            )
        )
        InterviewRepository(session, USER).add(
            Interview(
                id=INTERVIEW_ID,
                user_id=USER,
                resume_id=RESUME_ID,
                jd_text="高级后端工程师",
                type=InterviewType.TECHNICAL,
                style=InterviewStyle.GENTLE,
                language=Language.ZH,
                duration_min=20,
                status=InterviewStatus.PLANNED,
            )
        )
        PlanRepository(session, USER).add(
            Plan(
                interview_id=INTERVIEW_ID,
                user_id=USER,
                competencies_json=[item.model_dump(mode="json") for item in plan.competencies],
                questions_json=[item.model_dump(mode="json") for item in plan.questions],
                tech_terms_json=[],
                stages_json=[item.model_dump(mode="json") for item in plan.stages],
                prompt_version=plan.prompt_version,
            )
        )
        await session.commit()


@pytest.fixture
async def make_harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    created: list[Harness] = []

    async def build(
        *,
        actions: Sequence[str] | None = None,
        eval_default: str = "next_question",
        eval_delay_s: float = 0.0,
        eval_errors: Sequence[ProviderError] = (),
        front_reply: str = FRONT_REPLY,
        front_errors: Sequence[ProviderError] = (),
        config: OrchestratorConfig | None = None,
        budget_status: BudgetStatus = BudgetStatus.OK,
    ) -> Harness:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as conn:
            await create_tables(conn, User, Resume, Interview, Plan, Turn, Evaluation)
        sessionmaker = make_sessionmaker(engine)
        plan = make_plan()
        await seed(sessionmaker, plan)

        script = EvalScript(actions, eval_default)
        eval_llm = FakeLLM(
            reply=script.reply,
            model="fake-eval",
            chunk_size=10_000,
            first_token_delay_s=eval_delay_s,
            errors=eval_errors,
        )
        front = FakeLLM(
            reply=front_reply,
            model="fake-front",
            chunk_size=10_000,
            errors=front_errors,
        )
        settings = make_settings(user=USER)
        settings.providers.llm_front.provider = "fake"
        settings.providers.llm_front.model = "front"
        settings.providers.llm_live_eval.provider = "fake"
        settings.providers.llm_live_eval.model = "eval"

        def factory(role: Any) -> LLMProvider:
            return eval_llm if role.model == "eval" else front

        registry = ProviderRegistry(
            settings.providers,
            factories=ProviderFactories(llm={"fake": factory}),
        )
        budget_state = BudgetState(budget_status)

        async def check_budget(*_args: Any, **_kwargs: Any) -> BudgetCheck:
            return budget_result(budget_state.status)

        monkeypatch.setattr(budget, "check", check_budget)

        async def ignore_usage(**_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(usage, "record_usage", ignore_usage)

        ended: list[uuid.UUID] = []

        async def on_ended(interview_id: uuid.UUID) -> None:
            ended.append(interview_id)

        orchestrator = await InterviewOrchestrator.load(
            interview_id=INTERVIEW_ID,
            user_id=USER,
            sessionmaker=sessionmaker,
            providers=registry,
            config=config or DEFAULT_CONFIG,
            on_ended=on_ended,
        )
        harness = Harness(
            engine=engine,
            sessionmaker=sessionmaker,
            orchestrator=orchestrator,
            front=front,
            eval_llm=eval_llm,
            script=script,
            budget=budget_state,
            ended=ended,
            providers=registry,
            plan=plan,
        )
        created.append(harness)
        return harness

    yield build

    for harness in created:
        await harness.orchestrator.aclose()
        if harness.providers is not None:
            await harness.providers.aclose()
        await harness.engine.dispose()


async def collect(events: AsyncIterator[OrchestratorEvent]) -> list[OrchestratorEvent]:
    return [event async for event in events]


async def fetch_turns(harness: Harness) -> list[Turn]:
    async with harness.sessionmaker() as session:
        return list(
            (await session.scalars(TurnRepository(session, USER).select().order_by(Turn.seq))).all()
        )


async def fetch_evaluations(harness: Harness) -> list[Evaluation]:
    async with harness.sessionmaker() as session:
        return list((await session.scalars(EvaluationRepository(session, USER).select())).all())


async def fetch_interview(harness: Harness) -> Interview:
    async with harness.sessionmaker() as session:
        interview = await InterviewRepository(session, USER).get(INTERVIEW_ID)
        assert interview is not None
        return interview


def state_of(interview: Interview) -> InterviewState:
    assert interview.state_json is not None
    return InterviewState.model_validate(interview.state_json["machine"])


async def open_to_deep(harness: Harness) -> list[OrchestratorEvent]:
    events = await collect(harness.orchestrator.start(0))
    events += await collect(harness.orchestrator.on_command("skip", 1))
    events += await collect(harness.orchestrator.on_command("skip", 2))
    return events


def metric(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def turn_span(exporter: InMemorySpanExporter, kind: str) -> Any:
    spans = [
        span
        for span in exporter.get_finished_spans()
        if span.name == "interview.turn"
        and span.attributes is not None
        and span.attributes.get("turn.kind") == kind
    ]
    assert spans
    return spans[-1]


def events_of[T](events: Sequence[OrchestratorEvent], cls: type[T]) -> list[T]:
    return [event for event in events if isinstance(event, cls)]


async def test_full_interview_reaches_ended(
    make_harness: Any, exporter: InMemorySpanExporter
) -> None:
    harness = await make_harness(
        actions=["follow_up", "next_question", "next_question", "next_question"]
    )
    events = await open_to_deep(harness)
    for index in range(5):
        events += await collect(
            harness.orchestrator.on_user_turn(
                CANDIDATE_TEXT,
                10_000 + index * 10_000,
                20_000 + index * 10_000,
            )
        )
    events += await collect(harness.orchestrator.on_command("skip", 60_001))
    await harness.orchestrator.aclose()

    stages = [event.phase for event in events_of(events, StageEvent)]
    assert stages == [
        Phase.OPENING,
        Phase.SELF_INTRO,
        Phase.RESUME_DEEP_DIVE,
        Phase.TECHNICAL,
        Phase.CANDIDATE_QUESTIONS,
        Phase.CLOSING,
        Phase.ENDED,
    ]
    ended = events_of(events, EndedEvent)
    assert len(ended) == 1
    assert harness.ended == [INTERVIEW_ID]

    turns = await fetch_turns(harness)
    assert [turn.seq for turn in turns] == list(range(1, len(turns) + 1))
    assert sum(1 for turn in turns if turn.role is TurnRole.INTERVIEWER) > 0
    evaluations = await fetch_evaluations(harness)
    assert len(evaluations) == 5
    assert all(item.phase is EvaluationPhase.LIVE for item in evaluations)
    assert all(item.model == "fake/fake-eval" for item in evaluations)
    assert all(item.detail_json["rationale"] for item in evaluations)
    assert all(item.evidence_json for item in evaluations)
    candidate_ids = {turn.id for turn in turns if turn.role is TurnRole.CANDIDATE}
    assert all(item.turn_id in candidate_ids for item in evaluations)

    interview = await fetch_interview(harness)
    assert interview.status is InterviewStatus.COMPLETED
    assert interview.started_at is not None and interview.ended_at is not None
    session_spans = [
        span for span in exporter.get_finished_spans() if span.name == "interview.session"
    ]
    assert len(session_spans) == 1
    turn_spans = [span for span in exporter.get_finished_spans() if span.name == "interview.turn"]
    assert turn_spans
    session_span = session_spans[0]
    assert session_span.context is not None
    session_id = session_span.context.span_id
    assert all(span.parent is not None and span.parent.span_id == session_id for span in turn_spans)


async def test_start_sets_in_progress_and_persists_state(make_harness: Any) -> None:
    harness = await make_harness()
    events = await collect(harness.orchestrator.start(0))
    assert events_of(events, StageEvent)[0].phase is Phase.OPENING
    interview = await fetch_interview(harness)
    assert interview.status is InterviewStatus.IN_PROGRESS
    assert interview.started_at is not None
    assert interview.state_json is not None
    state = state_of(interview)
    assert state.phase is Phase.OPENING
    await harness.orchestrator.aclose()


async def test_pause_blocks_user_turn_until_resume(make_harness: Any) -> None:
    harness = await make_harness()
    await collect(harness.orchestrator.start(0))
    paused = await collect(harness.orchestrator.on_command("pause", 1_000))
    assert StateEvent(value="paused") in paused
    before_turns = await fetch_turns(harness)
    before = await fetch_interview(harness)
    ignored = await collect(harness.orchestrator.on_user_turn("暂停时说的话", 2_000, 3_000))
    assert ignored == []
    after_turns = await fetch_turns(harness)
    after = await fetch_interview(harness)
    assert len(after_turns) == len(before_turns) + 1
    assert after.state_json == before.state_json
    resumed = await collect(harness.orchestrator.on_command("resume", 4_000))
    assert StateEvent(value="listening") in resumed
    await harness.orchestrator.aclose()


async def test_repeat_skip_and_end(make_harness: Any) -> None:
    harness = await make_harness()
    await collect(harness.orchestrator.start(0))
    repeated = await collect(harness.orchestrator.on_command("repeat", 1_000))
    assert events_of(repeated, InterviewerDone)
    skipped = await collect(harness.orchestrator.on_command("skip", 2_000))
    assert events_of(skipped, StageEvent)[0].phase is Phase.SELF_INTRO
    # 说完收尾语就结束，不需要调用方再发 tick
    ending = await collect(harness.orchestrator.on_command("end", 3_000))
    assert [event.phase for event in events_of(ending, StageEvent)] == [Phase.CLOSING, Phase.ENDED]
    assert events_of(ending, EndedEvent)
    assert harness.ended == [INTERVIEW_ID]
    assert await collect(harness.orchestrator.on_tick(2_000_000)) == []
    await harness.orchestrator.aclose()
    interview = await fetch_interview(harness)
    assert interview.status is InterviewStatus.ABORTED
    assert interview.ended_at is not None


async def test_stage_timeout_emits_stage_event(make_harness: Any) -> None:
    harness = await make_harness()
    await collect(harness.orchestrator.start(0))
    events = await collect(harness.orchestrator.on_tick(90_001))
    stages = [event.phase for event in events_of(events, StageEvent)]
    assert stages == [Phase.SELF_INTRO]
    assert events_of(events, InterviewerDone)
    await harness.orchestrator.aclose()


async def test_think_and_check_in(make_harness: Any) -> None:
    harness = await make_harness()
    await collect(harness.orchestrator.start(0))
    events = await collect(harness.orchestrator.on_think(1_000))
    assert events == []
    interview = await fetch_interview(harness)
    thinking = state_of(interview)
    assert thinking.thinking_since_ms == thinking.active_ms

    events = await collect(harness.orchestrator.on_tick(21_000))
    assert events_of(events, InterviewerDone)
    interview = await fetch_interview(harness)
    assert state_of(interview).thinking_since_ms is None
    await harness.orchestrator.aclose()


async def test_eval_timeout_degrades_and_late_result_is_stored(
    make_harness: Any, exporter: InMemorySpanExporter
) -> None:
    before = metric("interview_orchestrator_eval_degraded_total", reason="timeout")
    harness = await make_harness(
        actions=["follow_up"],
        eval_delay_s=0.05,
        config=OrchestratorConfig(eval_wait_s=0.02),
    )
    await open_to_deep(harness)
    events = await collect(harness.orchestrator.on_user_turn(CANDIDATE_TEXT, 10_000, 20_000))
    assert metric("interview_orchestrator_eval_degraded_total", reason="timeout") == before + 1
    assert events_of(events, InterviewerDone)
    await harness.orchestrator.aclose()
    evaluations = await fetch_evaluations(harness)
    assert len(evaluations) == 1
    span = turn_span(exporter, "user_turn")
    assert span.attributes is not None
    assert span.attributes["orchestrator.eval_degraded"] is True


async def test_eval_error_degrades_without_storing(make_harness: Any) -> None:
    before = metric("interview_orchestrator_eval_degraded_total", reason="error")
    harness = await make_harness(
        actions=["follow_up"],
        eval_errors=[ProviderTimeout("timeout", provider="fake", model="fake-eval")],
    )
    await open_to_deep(harness)
    events = await collect(harness.orchestrator.on_user_turn(CANDIDATE_TEXT, 10_000, 20_000))
    assert metric("interview_orchestrator_eval_degraded_total", reason="error") == before + 1
    assert events_of(events, InterviewerDone)
    await harness.orchestrator.aclose()
    assert await fetch_evaluations(harness) == []


async def test_follow_up_limit_turns_into_next_question(make_harness: Any) -> None:
    harness = await make_harness(eval_default="follow_up")
    await open_to_deep(harness)
    for index in range(3):
        await collect(
            harness.orchestrator.on_user_turn(
                CANDIDATE_TEXT,
                10_000 + index * 10_000,
                20_000 + index * 10_000,
            )
        )
    await harness.orchestrator.aclose()
    evaluations = await fetch_evaluations(harness)
    assert evaluations[-1].next_action is NextAction.NEXT_QUESTION
    assert evaluations[-1].detail_json["override_reason"] == "follow_up_limit"
    interview = await fetch_interview(harness)
    machine = state_of(interview)
    assert machine.current_question_id == "q2"


async def test_budget_warn_keeps_decision(
    make_harness: Any, exporter: InMemorySpanExporter
) -> None:
    harness = await make_harness(actions=["next_question"], budget_status=BudgetStatus.WARN)
    await open_to_deep(harness)
    events = await collect(harness.orchestrator.on_user_turn(CANDIDATE_TEXT, 10_000, 20_000))
    assert events_of(events, InterviewerDone)
    await harness.orchestrator.aclose()
    interview = await fetch_interview(harness)
    machine = state_of(interview)
    assert machine.phase is Phase.RESUME_DEEP_DIVE
    assert machine.current_question_id == "q2"
    span = turn_span(exporter, "user_turn")
    assert span.attributes is not None
    assert span.attributes["budget.status"] == "warn"


async def test_budget_exceeded_wraps_up(make_harness: Any, exporter: InMemorySpanExporter) -> None:
    harness = await make_harness(actions=["next_question"], budget_status=BudgetStatus.EXCEEDED)
    await open_to_deep(harness)
    events = await collect(harness.orchestrator.on_user_turn(CANDIDATE_TEXT, 10_000, 20_000))
    phases = [event.phase for event in events_of(events, StageEvent)]
    assert phases[-2:] == [Phase.CLOSING, Phase.ENDED]
    await harness.orchestrator.aclose()
    interview = await fetch_interview(harness)
    assert state_of(interview).phase is Phase.ENDED
    assert interview.status is InterviewStatus.COMPLETED
    span = turn_span(exporter, "user_turn")
    assert span.attributes is not None
    assert span.attributes["budget.status"] == "exceeded"


async def test_state_can_be_reloaded_and_continued(make_harness: Any) -> None:
    harness = await make_harness(actions=["next_question"])
    await open_to_deep(harness)
    await collect(harness.orchestrator.on_user_turn(CANDIDATE_TEXT, 10_000, 20_000))
    live_state = harness.orchestrator.state
    assert live_state is not None
    interview = await fetch_interview(harness)
    assert interview.state_json is not None
    restored = state_of(interview)
    assert restored == live_state

    second = await InterviewOrchestrator.load(
        interview_id=INTERVIEW_ID,
        user_id=USER,
        sessionmaker=harness.sessionmaker,
        providers=harness.providers,
        config=DEFAULT_CONFIG,
    )
    try:
        assert second._state == restored
        events = await collect(second.on_command("skip", 30_000))
        assert events_of(events, StageEvent)
    finally:
        await second.aclose()
    await harness.orchestrator.aclose()


async def test_entries_are_serialized_and_lock_released_on_aclose(make_harness: Any) -> None:
    harness = await make_harness()
    await collect(harness.orchestrator.start(0))
    first = harness.orchestrator.on_command("skip", 1_000)
    first_event = await anext(first)
    assert first_event is not None
    await first.aclose()  # 只取了第一个事件就关闭，锁必须释放
    second = await collect(harness.orchestrator.on_command("skip", 2_000))
    assert events_of(second, StageEvent)
    await harness.orchestrator.aclose()


async def test_transition_sentence_is_first_and_in_turn_text(make_harness: Any) -> None:
    harness = await make_harness(actions=["next_question"])
    await open_to_deep(harness)
    events = await collect(harness.orchestrator.on_user_turn(CANDIDATE_TEXT, 10_000, 20_000))
    transitions = events_of(events, InterviewerSentence)
    assert transitions and transitions[0].transition is True
    turns = await fetch_turns(harness)
    interviewer = [turn for turn in turns if turn.role is TurnRole.INTERVIEWER]
    assert interviewer[-1].text.startswith(transitions[0].text)
    await harness.orchestrator.aclose()


async def test_no_transition_when_candidate_speaks_during_closing(make_harness: Any) -> None:
    harness = await make_harness()
    await collect(harness.orchestrator.start(0))
    budget_state = harness.budget
    budget_state.status = BudgetStatus.OK
    # 进入 closing 但还没结束：直接把状态机推到 closing，模拟收尾语播放时候选人插话
    orchestrator = harness.orchestrator
    plan, state = orchestrator._require()  # pyright: ignore[reportPrivateUsage]
    orchestrator._state = state.model_copy(  # pyright: ignore[reportPrivateUsage]
        update={"phase": Phase.CLOSING, "current_question_id": None}
    )
    assert plan is not None
    events = await collect(orchestrator.on_user_turn("好的，谢谢", 5_000, 6_000))
    assert not [event for event in events_of(events, InterviewerSentence) if event.transition]
    assert events_of(events, EndedEvent)
    await orchestrator.aclose()


async def test_transition_time_counts_toward_eval_deadline(make_harness: Any) -> None:
    # 上限 0.2 秒，评估要 0.25 秒，消费方处理过渡语（语音层在播）用了 0.1 秒。
    # 从评估开始计时：0.2 秒时截止，此时评估还没好，降级，整轮约 0.2 秒结束。
    # 若错误地从过渡语之后才计时：会等到 0.1 + 0.2 = 0.3 秒，评估在 0.25 秒就好了，不降级。
    before = metric("interview_orchestrator_eval_degraded_total", reason="timeout")
    harness = await make_harness(
        actions=["follow_up"], eval_delay_s=0.25, config=OrchestratorConfig(eval_wait_s=0.2)
    )
    await open_to_deep(harness)
    started = time.perf_counter()
    async for event in harness.orchestrator.on_user_turn(CANDIDATE_TEXT, 10_000, 20_000):
        if isinstance(event, InterviewerSentence) and event.transition:
            await asyncio.sleep(0.1)
    assert time.perf_counter() - started < 0.25
    assert metric("interview_orchestrator_eval_degraded_total", reason="timeout") == before + 1
    await harness.orchestrator.aclose()


async def test_slow_budget_check_does_not_block_and_is_awaited_on_close(
    make_harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = await make_harness(actions=["next_question"])
    finished: list[bool] = []

    async def slow_check(*_args: Any, **_kwargs: Any) -> BudgetCheck:
        await asyncio.sleep(1.0)
        finished.append(True)
        return budget_result(BudgetStatus.EXCEEDED)

    monkeypatch.setattr(budget, "check", slow_check)
    await open_to_deep(harness)
    started = time.perf_counter()
    events = await collect(harness.orchestrator.on_user_turn(CANDIDATE_TEXT, 10_000, 20_000))
    assert time.perf_counter() - started < 0.9  # 没有等满 1 秒的预算查询
    assert not events_of(events, EndedEvent)  # 超时按 ok 处理，没有因 exceeded 收尾
    await harness.orchestrator.aclose()
    assert finished == [True]  # aclose() 等后台的预算查询跑完


async def test_session_span_ends_after_last_turn_span(
    make_harness: Any, exporter: InMemorySpanExporter
) -> None:
    harness = await make_harness()
    await collect(harness.orchestrator.start(0))
    await collect(harness.orchestrator.on_command("end", 1_000))
    await harness.orchestrator.aclose()
    spans = exporter.get_finished_spans()
    (session,) = [span for span in spans if span.name == "interview.session"]
    turns = [span for span in spans if span.name == "interview.turn"]
    assert session.end_time is not None
    assert all(span.end_time is not None and span.end_time <= session.end_time for span in turns)


async def test_front_error_uses_fallback_and_continues(make_harness: Any) -> None:
    before = metric("interview_orchestrator_front_fallback_total")
    harness = await make_harness(
        front_errors=[ProviderUnavailable("down", provider="fake", model="fake-front")]
    )
    events = await collect(harness.orchestrator.start(0))
    assert any(
        "不好意思，我们继续。" in event.text for event in events_of(events, InterviewerSentence)
    )
    assert metric("interview_orchestrator_front_fallback_total") == before + 1
    continued = await collect(harness.orchestrator.on_command("skip", 1_000))
    assert events_of(continued, StageEvent)
    await harness.orchestrator.aclose()
    turns = await fetch_turns(harness)
    assert any("不好意思，我们继续。" in turn.text for turn in turns)


async def test_logs_do_not_contain_dialogue(make_harness: Any) -> None:
    harness = await make_harness(actions=["next_question"], front_reply="PRIVATE_FRONT_TEXT。")
    with capture_logs() as logs:
        await open_to_deep(harness)
        await collect(
            harness.orchestrator.on_user_turn("PRIVATE_CANDIDATE_TEXT 本地消息表", 10_000, 20_000)
        )
    await harness.orchestrator.aclose()
    logged = json.dumps(logs, ensure_ascii=False, default=str)
    assert "PRIVATE_CANDIDATE_TEXT" not in logged
    assert "PRIVATE_FRONT_TEXT" not in logged
