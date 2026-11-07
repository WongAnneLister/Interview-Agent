"""跑一场评测面试：准备简历和计划 → 驱动编排器 → 生成报告。

所有厂商调用都在 eval_mode() 里，用量带 is_eval；整场面试是 Langfuse 里一条带 eval 标签的 trace。
"""

from __future__ import annotations

import asyncio
import random
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evals.candidate import SimulatedCandidate
from evals.data import Fixture, Persona
from evals.driver import Driver, DriveResult
from interview.config import ReportSettings
from interview.db.models import Resume, UsageRole
from interview.domain.interview_setup import InterviewSettings, build_plan, persist_interview
from interview.domain.orchestrator import InterviewOrchestrator
from interview.domain.report import generate_report
from interview.domain.resume import ResumeParsed, ResumeRepository, parse_resume
from interview.observability.tracing import span
from interview.providers.observed import CallContext, eval_mode
from interview.providers.registry import ProviderRegistry

EVAL_DURATION_MIN = 15  # 评测统一用 15 分钟，省钱

log = structlog.get_logger(__name__)


@dataclass
class EvalContext:
    sessionmaker: async_sessionmaker[AsyncSession]
    providers: ProviderRegistry
    user_id: uuid.UUID
    report: ReportSettings
    seed: int = 0
    _resumes: dict[str, tuple[uuid.UUID, ResumeParsed]] = field(
        default_factory=dict[str, tuple[uuid.UUID, ResumeParsed]]
    )
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict[str, asyncio.Lock])

    def ctx(self, role: UsageRole, interview_id: uuid.UUID | None = None) -> CallContext:
        return CallContext(user_id=self.user_id, interview_id=interview_id, usage_role=role)

    async def resume(self, fixture: Fixture) -> tuple[uuid.UUID, ResumeParsed]:
        """同一次评测里每份简历只解析一次，多个人设共用。"""
        lock = self._locks.setdefault(fixture.id, asyncio.Lock())
        async with lock:
            if fixture.id not in self._resumes:
                llm = self.providers.llm("llm_eval", self.ctx(UsageRole.RESUME_PARSE))
                parsed = (await parse_resume(llm, fixture.resume)).value
                row = Resume(
                    user_id=self.user_id,
                    file_key=f"eval/{fixture.id}",  # 评测简历没有原件
                    parsed_json=parsed.model_dump(),
                )
                async with self.sessionmaker() as session:
                    ResumeRepository(session, self.user_id).add(row)
                    await session.commit()
                    await session.refresh(row)
                self._resumes[fixture.id] = (row.id, parsed)
            return self._resumes[fixture.id]


@dataclass
class SessionOutcome:
    fixture: str
    persona: str
    interview_id: uuid.UUID
    trace_id: str = ""
    drive: DriveResult = field(default_factory=DriveResult)
    error: str | None = None
    duration_s: float = 0.0


async def run_session(ec: EvalContext, fixture: Fixture, persona: Persona) -> SessionOutcome:
    interview_id = uuid.uuid4()
    outcome = SessionOutcome(fixture.id, persona.id, interview_id)
    started = time.perf_counter()
    with (
        eval_mode(),
        span(
            "eval.session",
            interview_id=str(interview_id),
            is_eval=True,
        ) as current,
    ):
        current.set_attributes({"eval.fixture": fixture.id, "eval.persona": persona.id})
        outcome.trace_id = format(current.get_span_context().trace_id, "032x")
        try:
            outcome.drive = await _run(ec, fixture, persona, interview_id)
        except Exception as exc:
            outcome.error = describe(exc)
            log.warning(
                "eval_session_failed",
                interview_id=str(interview_id),
                fixture=fixture.id,
                persona=persona.id,
                error=type(exc).__name__,
                exc_info=True,
            )
    outcome.duration_s = round(time.perf_counter() - started, 1)
    return outcome


def describe(exc: BaseException) -> str:
    """错误类型、信息和抛出位置（项目代码里最近的一帧）；TimeoutError 之类没有信息时靠位置定位。"""
    frames = traceback.extract_tb(exc.__traceback__)
    project = ("/src/interview/", "/evals/", "/tests/")
    ours = [f for f in frames if any(part in f.filename for part in project)] or frames
    where = f" @ {Path(ours[-1].filename).name}:{ours[-1].lineno} {ours[-1].name}" if ours else ""
    message = str(exc).strip()
    return f"{type(exc).__name__}{': ' + message if message else ''}{where}"


async def _run(
    ec: EvalContext, fixture: Fixture, persona: Persona, interview_id: uuid.UUID
) -> DriveResult:
    resume_id, resume = await ec.resume(fixture)
    settings = InterviewSettings(
        resume_id=resume_id,
        jd_text=fixture.jd,
        type=fixture.type,
        style=persona.style or fixture.style,
        language=fixture.language,
        duration_min=EVAL_DURATION_MIN,
    )
    plan = await build_plan(
        jd_llm=ec.providers.llm("llm_eval", ec.ctx(UsageRole.JD_PARSE, interview_id)),
        plan_llm=ec.providers.llm("llm_eval", ec.ctx(UsageRole.PLAN, interview_id)),
        resume=resume,
        settings=settings,
        interview_id=interview_id,
    )
    await persist_interview(
        ec.sessionmaker,
        user_id=ec.user_id,
        interview_id=interview_id,
        settings=settings,
        plan=plan.value,
    )
    orchestrator = await InterviewOrchestrator.load(
        interview_id=interview_id,
        user_id=ec.user_id,
        sessionmaker=ec.sessionmaker,
        providers=ec.providers,
    )
    candidate = SimulatedCandidate(
        llm=ec.providers.live_eval_llm(ec.ctx(UsageRole.EVAL_HARNESS, interview_id)),
        persona=persona,
        resume=fixture.resume,
        language=fixture.language,
        rng=random.Random(f"{ec.seed}:{fixture.id}:{persona.id}"),
    )
    try:
        drive = await Driver(orchestrator, candidate, fixture.language).run()
    finally:
        await orchestrator.aclose()
    await generate_report(
        interview_id=interview_id,
        user_id=ec.user_id,
        sessionmaker=ec.sessionmaker,
        providers=ec.providers,
        settings=ec.report,
    )
    return drive
