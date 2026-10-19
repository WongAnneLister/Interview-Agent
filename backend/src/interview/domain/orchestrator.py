"""文字面试编排：串起状态机、评估器和前台面试官，与传输方式无关（文字和语音共用）。

事件定义见 orchestrator_events.py，数据库读写见 orchestrator_store.py。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from contextlib import aclosing, contextmanager
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.db.models import InterviewStatus, InterviewStyle, Language, TurnRole, UsageRole
from interview.domain.evaluator import EvaluationInput, TranscriptTurn
from interview.domain.history import TurnRecord, build_history
from interview.domain.interviewer import TransitionPicker
from interview.domain.orchestrator_eval import TurnBackground
from interview.domain.orchestrator_events import (
    DEFAULT_CONFIG,
    CommandName,
    EndedEvent,
    InterviewerDelta,
    InterviewerDone,
    InterviewerSentence,
    OrchestratorConfig,
    OrchestratorEvent,
    StageEvent,
    StateEvent,
)
from interview.domain.orchestrator_speech import Speech, separator_for
from interview.domain.orchestrator_store import InterviewStore
from interview.domain.plan import InterviewPlan, PlanCompetency, Question
from interview.domain.state_machine import (
    Command,
    Decision,
    Directive,
    InterviewState,
    Phase,
    Step,
    Think,
    Tick,
    stage_remaining_ms,
    total_remaining_ms,
)
from interview.domain.state_machine import start as machine_start
from interview.domain.state_machine import step as machine_step
from interview.observability.budget import BudgetStatus
from interview.observability.tracing import OpenSpan, open_span
from interview.providers.observed import CallContext
from interview.providers.registry import ProviderRegistry

__all__ = [
    "DEFAULT_CONFIG",
    "EndedEvent",
    "InterviewOrchestrator",
    "InterviewerDelta",
    "InterviewerDone",
    "InterviewerSentence",
    "OrchestratorConfig",
    "OrchestratorEvent",
    "StageEvent",
    "StateEvent",
]

log = structlog.get_logger(__name__)


class InterviewOrchestrator:
    def __init__(
        self,
        *,
        interview_id: uuid.UUID,
        user_id: uuid.UUID,
        sessionmaker: async_sessionmaker[AsyncSession],
        providers: ProviderRegistry,
        config: OrchestratorConfig = DEFAULT_CONFIG,
        on_ended: Callable[[uuid.UUID], Awaitable[None]] | None = None,
    ) -> None:
        self._interview_id = interview_id
        self._user_id = user_id
        self._sessionmaker = sessionmaker
        self._providers = providers
        self._config = config
        self._on_ended = on_ended
        self._lock = asyncio.Lock()
        self._store = InterviewStore(sessionmaker, interview_id=interview_id, user_id=user_id)
        self._plan: InterviewPlan | None = None
        self._state: InterviewState | None = None
        self._style = InterviewStyle.GENTLE
        self._language = Language.ZH
        self._picker = TransitionPicker(self._language, self._style)
        self._turns: list[TurnRecord] = []
        self._question_turns: dict[str, list[TranscriptTurn]] = {}
        self._ended_by_user = False
        self._ended_called = False
        self._session_span: OpenSpan | None = None
        self._pending: set[asyncio.Task[Any]] = set()
        self._closed = False

    @classmethod
    async def load(
        cls,
        *,
        interview_id: uuid.UUID,
        user_id: uuid.UUID,
        sessionmaker: async_sessionmaker[AsyncSession],
        providers: ProviderRegistry,
        config: OrchestratorConfig = DEFAULT_CONFIG,
        on_ended: Callable[[uuid.UUID], Awaitable[None]] | None = None,
    ) -> InterviewOrchestrator:
        orchestrator = cls(
            interview_id=interview_id,
            user_id=user_id,
            sessionmaker=sessionmaker,
            providers=providers,
            config=config,
            on_ended=on_ended,
        )
        loaded = await orchestrator._store.load()
        orchestrator._plan = loaded.plan
        orchestrator._state = loaded.state
        orchestrator._style = loaded.style
        orchestrator._language = loaded.language
        orchestrator._picker = TransitionPicker(loaded.language, loaded.style)
        orchestrator._turns = loaded.turns
        orchestrator._question_turns = loaded.question_turns
        orchestrator._ended_by_user = loaded.ended_by_user
        return orchestrator

    @property
    def state(self) -> InterviewState | None:
        """当前状态机状态；还没 start() 时为空。"""
        return self._state

    def _require(self) -> tuple[InterviewPlan, InterviewState]:
        if self._plan is None or self._state is None:
            raise RuntimeError("面试还没开始")
        return self._plan, self._state

    def _current_question(self) -> Question | None:
        plan, state = self._require()
        if state.current_question_id is None:
            return None
        return next(
            (question for question in plan.questions if question.id == state.current_question_id),
            None,
        )

    def _competency_for(self, question: Question) -> PlanCompetency | None:
        plan, _ = self._require()
        return next(
            (item for item in plan.competencies if item.name == question.competency),
            None,
        )

    def _stage_event(self) -> StageEvent:
        plan, state = self._require()
        return StageEvent(
            phase=state.phase,
            remaining_s=max(0, stage_remaining_ms(plan, state)) // 1000,
            total_remaining_s=max(0, total_remaining_ms(plan, state)) // 1000,
        )

    @contextmanager
    def _turn_span(self, kind: str) -> Generator[OpenSpan]:
        if self._session_span is not None:
            with self._session_span.activate():
                span = open_span("interview.turn", interview_id=str(self._interview_id))
        else:
            span = open_span("interview.turn", interview_id=str(self._interview_id))
        span.span.set_attribute("turn.kind", kind)
        span.span.set_attribute("directive.kind", "")
        span.span.set_attribute("orchestrator.eval_degraded", False)
        span.span.set_attribute("orchestrator.eval_wait_ms", 0)
        span.span.set_attribute("budget.status", BudgetStatus.OK.value)
        try:
            yield span
        except (asyncio.CancelledError, GeneratorExit):
            span.end(None)
            raise
        except BaseException as exc:
            span.end(exc)
            raise
        else:
            span.end(None)
        finally:
            # 面试结束时，会话 span 在最后一轮的 span 之后结束，trace 时间线才正确
            if self._ended_called:
                self._end_session_span()

    def _end_session_span(self) -> None:
        if self._session_span is not None:
            self._session_span.end()
            self._session_span = None

    async def _append_turn(
        self,
        *,
        role: TurnRole,
        text: str,
        question_id: str | None,
        phase: Phase,
        start_ms: int,
        end_ms: int,
    ) -> uuid.UUID:
        turn_id = await self._store.append_turn(
            role=role, text=text, question_id=question_id, start_ms=start_ms, end_ms=end_ms
        )
        self._turns.append(TurnRecord(role=role, text=text, question_id=question_id, phase=phase))
        if question_id is not None:
            self._question_turns.setdefault(question_id, []).append(
                TranscriptTurn(
                    turn_id=turn_id,
                    role=role,
                    text=text,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
            )
        return turn_id

    async def _save_state(
        self,
        *,
        status: InterviewStatus | None = None,
        started: bool = False,
        ended: bool = False,
    ) -> None:
        _, state = self._require()
        await self._store.save_state(
            state, ended_by_user=self._ended_by_user, status=status, started=started, ended=ended
        )

    def _track(self, task: asyncio.Task[Any]) -> None:
        """后台任务登记到 _pending，完成后自动移除；aclose() 会等它们跑完。"""
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _record_ignored_turn(self, text: str, start_ms: int, end_ms: int) -> None:
        """暂停中或已结束时的发言：照样写入转写，但不推进状态。"""
        _, state = self._require()
        log.warning(
            "orchestrator_user_turn_ignored",
            interview_id=str(self._interview_id),
            phase=state.phase.value,
            paused=state.paused,
        )
        question_id = None if state.phase is Phase.ENDED else state.current_question_id
        await self._append_turn(
            role=TurnRole.CANDIDATE,
            text=text,
            question_id=question_id,
            phase=state.phase,
            start_ms=start_ms,
            end_ms=end_ms,
        )

    async def _speak_events(
        self,
        directive: Directive,
        *,
        trigger_ms: int,
        transition: str,
        span: OpenSpan,
    ) -> AsyncGenerator[OrchestratorEvent, None]:
        plan, state = self._require()
        yield StateEvent(value="speaking")
        front = self._providers.llm(
            "llm_front",
            CallContext(
                user_id=self._user_id,
                interview_id=self._interview_id,
                usage_role=UsageRole.FRONT,
            ),
        )
        speech = Speech(
            front,
            directive,
            style=self._style,
            language=self._language,
            history=build_history(self._turns, plan, state.phase, language=self._language),
            interview_id=self._interview_id,
            span=span,
        )
        async with aclosing(speech.events()) as events:
            async for event in events:
                yield event
        separator = separator_for(self._language)
        spoken = speech.text
        full_text = (
            f"{transition}{separator}{spoken}" if transition and spoken else transition or spoken
        )
        turn_id = await self._append_turn(
            role=TurnRole.INTERVIEWER,
            text=full_text,
            question_id=state.current_question_id,
            phase=state.phase,
            start_ms=trigger_ms,
            end_ms=trigger_ms,
        )
        yield InterviewerDone(turn_id=turn_id, text=full_text)
        yield StateEvent(value="paused" if state.paused else "listening")

    async def _finish_ended(self) -> None:
        if self._ended_called:
            return
        self._ended_called = True
        status = InterviewStatus.ABORTED if self._ended_by_user else InterviewStatus.COMPLETED
        try:
            await self._save_state(status=status, ended=True)
        except Exception as exc:
            log.warning(
                "orchestrator_end_save_failed",
                interview_id=str(self._interview_id),
                error=type(exc).__name__,
            )
        if self._on_ended is not None:
            try:
                await self._on_ended(self._interview_id)
            except Exception as exc:
                log.warning(
                    "orchestrator_on_ended_failed",
                    interview_id=str(self._interview_id),
                    error=type(exc).__name__,
                )

    async def _respond(
        self,
        step: Step,
        *,
        previous_phase: Phase,
        trigger_ms: int,
        transition: str,
        span: OpenSpan,
    ) -> AsyncGenerator[OrchestratorEvent, None]:
        self._state = step.state
        span.span.set_attribute("state.phase", step.state.phase.value)
        if step.directive is not None:
            span.span.set_attribute("directive.kind", step.directive.kind)
            if step.directive.kind == "closing" and step.directive.reason == "user_end":
                self._ended_by_user = True
        if step.state.phase != previous_phase:
            yield self._stage_event()
        if step.directive is not None:
            async with aclosing(
                self._speak_events(
                    step.directive,
                    trigger_ms=trigger_ms,
                    transition=transition,
                    span=span,
                )
            ) as events:
                async for event in events:
                    yield event
        if step.directive is not None and step.directive.kind == "closing":
            # 收尾语说完就结束，不等调用方再发 tick：否则状态停在 in_progress，报告也不会生成
            plan, _ = self._require()
            step = machine_step(
                plan,
                step.state,
                Decision(now_ms=step.state.clock_ms, action="next_question"),
                self._config.machine,
            )
            self._state = step.state
            yield self._stage_event()
        if step.state.phase is Phase.ENDED:
            await self._finish_ended()
            yield EndedEvent()
        else:
            await self._save_state()

    async def start(self, now_ms: int) -> AsyncGenerator[OrchestratorEvent, None]:
        async with self._lock:
            if self._state is not None:
                return
            plan = self._require_plan()
            self._session_span = open_span(
                "interview.session", interview_id=str(self._interview_id)
            )
            with self._turn_span("start") as span:
                step = machine_start(plan, now_ms, self._config.machine)
                self._state = step.state
                span.span.set_attribute("state.phase", step.state.phase.value)
                await self._save_state(status=InterviewStatus.IN_PROGRESS, started=True)
                yield self._stage_event()
                if step.directive is not None:
                    span.span.set_attribute("directive.kind", step.directive.kind)
                    async with aclosing(
                        self._speak_events(
                            step.directive,
                            trigger_ms=now_ms,
                            transition="",
                            span=span,
                        )
                    ) as events:
                        async for event in events:
                            yield event
                if step.state.phase is Phase.ENDED:
                    await self._finish_ended()
                    yield EndedEvent()

    async def on_user_turn(
        self, text: str, start_ms: int, end_ms: int
    ) -> AsyncGenerator[OrchestratorEvent, None]:
        async with self._lock:
            plan, state = self._require()
            if state.phase is Phase.ENDED:
                # 结束后的发言照样记录，但不再开 span：会话 span 已经结束
                await self._record_ignored_turn(text, start_ms, end_ms)
                return
            with self._turn_span("user_turn") as span:
                span.span.set_attribute("state.phase", state.phase.value)
                if state.paused:
                    await self._record_ignored_turn(text, start_ms, end_ms)
                    return
                question = self._current_question()
                candidate_turn_id = await self._append_turn(
                    role=TurnRole.CANDIDATE,
                    text=text,
                    question_id=state.current_question_id,
                    phase=state.phase,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
                yield StateEvent(value="thinking")

                evaluation = None
                competency = self._competency_for(question) if question is not None else None
                if question is not None and competency is not None:
                    data = EvaluationInput(
                        question=question,
                        competency=competency,
                        turns=list(self._question_turns.get(question.id, [])),
                        follow_ups=state.follow_ups,
                        max_follow_ups=self._config.machine.max_follow_ups,
                        stage_remaining_ms=max(0, stage_remaining_ms(plan, state)),
                    )
                    llm = self._providers.live_eval_llm(
                        CallContext(
                            user_id=self._user_id,
                            interview_id=self._interview_id,
                            turn_id=candidate_turn_id,
                            usage_role=UsageRole.EVAL,
                        )
                    )
                    evaluation = (llm, data, candidate_turn_id)
                background = TurnBackground(
                    interview_id=self._interview_id,
                    user_id=self._user_id,
                    sessionmaker=self._sessionmaker,
                    store=self._store,
                    track=self._track,
                    span=span,
                    evaluation=evaluation,
                )

                transition = ""
                if state.phase is not Phase.CLOSING:
                    transition = self._picker.pick()
                    yield InterviewerSentence(text=transition, transition=True)

                decision = await background.decide(self._config.eval_wait_s)
                with span.activate():
                    step = machine_step(
                        plan,
                        state,
                        Decision(now_ms=end_ms, action=decision.action, focus=decision.focus),
                        self._config.machine,
                    )
                async with aclosing(
                    self._respond(
                        step,
                        previous_phase=state.phase,
                        trigger_ms=end_ms,
                        transition=transition,
                        span=span,
                    )
                ) as events:
                    async for event in events:
                        yield event

    def on_command(
        self, command: CommandName, now_ms: int
    ) -> AsyncGenerator[OrchestratorEvent, None]:
        return self._run_event("command", Command(now_ms=now_ms, command=command))

    def on_think(self, now_ms: int) -> AsyncGenerator[OrchestratorEvent, None]:
        """候选人说“我想一下”（PR-30 调用）。"""
        return self._run_event("think", Think(now_ms=now_ms))

    def on_tick(self, now_ms: int) -> AsyncGenerator[OrchestratorEvent, None]:
        """时间推进：阶段超时、思考超时由状态机判断。"""
        return self._run_event("tick", Tick(now_ms=now_ms))

    async def _run_event(
        self, kind: str, event: Command | Think | Tick
    ) -> AsyncGenerator[OrchestratorEvent, None]:
        async with self._lock:
            plan, state = self._require()
            if state.phase is Phase.ENDED:
                return
            with self._turn_span(kind) as span:
                span.span.set_attribute("state.phase", state.phase.value)
                if isinstance(event, Command) and event.command == "end":
                    self._ended_by_user = True
                with span.activate():
                    step = machine_step(plan, state, event, self._config.machine)
                async with aclosing(
                    self._respond(
                        step,
                        previous_phase=state.phase,
                        trigger_ms=event.now_ms,
                        transition="",
                        span=span,
                    )
                ) as events:
                    async for item in events:
                        yield item

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        pending = list(self._pending)
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=self._config.eval_wait_s * 4,
                )
            except TimeoutError:
                log.warning(
                    "orchestrator_pending_timeout",
                    interview_id=str(self._interview_id),
                    pending=len(pending),
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        self._end_session_span()

    def _require_plan(self) -> InterviewPlan:
        if self._plan is None:
            raise RuntimeError("面试计划不存在")
        return self._plan
