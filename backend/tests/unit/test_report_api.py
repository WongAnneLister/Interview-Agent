"""报告 API：SQLite 状态、用户隔离、超时兜底和重新生成。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlite_helpers import create_tables
from test_api import error_of, make_settings, open_client
from test_orchestrator import INTERVIEW_ID, USER, make_plan, seed

from interview.api.app import create_app
from interview.api.deps import routes_without_user
from interview.api.resources import Resources, create_resources
from interview.api.routes import text_ws
from interview.config import Settings
from interview.db.models import (
    Interview,
    InterviewStatus,
    Plan,
    Report,
    ReportStatus,
    Resume,
    Turn,
    TurnRole,
    User,
)
from interview.db.repository import UserScopedRepository
from interview.db.session import make_sessionmaker
from interview.domain.orchestrator import InterviewOrchestrator
from interview.domain.orchestrator_store import TurnRepository
from interview.domain.plan import InterviewRepository
from interview.providers.registry import ProviderRegistry

URL = f"/api/interviews/{INTERVIEW_ID}/report"
DISCLAIMER = "AI 生成，仅供练习参考，不代表任何企业的录用判断。"
BODY = {
    "schema_version": 1,
    "overall_score": None,
    "overall": "本场未考察到",
    "top_improvements": [],
    "competencies": [],
    "questions": [],
    "disclaimer": DISCLAIMER,
    "degraded": [],
}


class ReportRows(UserScopedRepository[Report]):
    model = Report


@dataclass
class FakeRunner:
    sessionmaker: Any = None  # request_report 用它写 pending 行
    active: bool = False
    starts: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)

    def start(self, interview_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        if self.active:
            return False
        self.active = True
        self.starts.append((interview_id, user_id))
        return True

    def is_running(self, _interview_id: uuid.UUID) -> bool:
        return self.active

    async def aclose(self) -> None:
        return None


@dataclass
class Api:
    client: httpx.AsyncClient
    sessionmaker: async_sessionmaker[AsyncSession]
    settings: Settings
    runner: FakeRunner
    resources: Resources

    async def status(self, status: InterviewStatus) -> None:
        async with self.sessionmaker() as session:
            row = await InterviewRepository(session, USER).get(INTERVIEW_ID)
            assert row is not None
            row.status = status
            await session.commit()

    async def report(self, status: ReportStatus, *, age_s: int = 0) -> None:
        async with self.sessionmaker() as session:
            ReportRows(session, USER).add(
                Report(
                    interview_id=INTERVIEW_ID,
                    user_id=USER,
                    status=status,
                    summary_json=BODY
                    if status == ReportStatus.READY
                    else {"error": "ReportTimeout"},
                    generated_at=datetime.now(UTC) if status == ReportStatus.READY else None,
                    model="fake/report",
                    prompt_version="report_question@1",
                    updated_at=datetime.now(UTC) - timedelta(seconds=age_s),
                )
            )
            await session.commit()


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await create_tables(conn, User, Resume, Interview, Plan, Report, Turn)
    sm = make_sessionmaker(engine)
    await seed(sm, make_plan())
    settings = make_settings(user=USER)
    runner = FakeRunner(sessionmaker=sm)
    resources = Resources(
        settings=settings, sessionmaker=sm, providers=ProviderRegistry(settings.providers)
    )
    resources.reports = cast(Any, runner)
    app = create_app(settings, resource_factory=lambda _s: resources, init_observability=False)
    assert routes_without_user(app) == []
    async with open_client(app) as client:
        api = Api(client, sm, settings, runner, resources)
        await api.status(InterviewStatus.COMPLETED)
        yield api
    await engine.dispose()


@pytest.mark.parametrize("status", list(ReportStatus))
async def test_get_report_status(api: Api, status: ReportStatus) -> None:
    await api.report(status)
    response = await api.client.get(URL)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == status.value
    assert body["disclaimer"] == DISCLAIMER
    assert body["report"] == (BODY if status == ReportStatus.READY else None)
    assert body["error"] == ("ReportTimeout" if status == ReportStatus.FAILED else None)
    assert bool(body["generated_at"]) == (status == ReportStatus.READY)


@pytest.mark.parametrize("status", [InterviewStatus.COMPLETED, InterviewStatus.ABORTED])
async def test_transcript_returns_ordered_fields_for_this_interview(
    api: Api, status: InterviewStatus
) -> None:
    await api.status(status)
    turns = [
        Turn(
            id=uuid.uuid4(),
            interview_id=INTERVIEW_ID,
            user_id=USER,
            seq=seq,
            role=role,
            text=text,
            start_ms=seq * 100,
            end_ms=seq * 100 + 90,
            question_id=question,
        )
        for seq, role, text, question in [
            (2, TurnRole.CANDIDATE, "我的回答", "q1"),
            (1, TurnRole.INTERVIEWER, "开场", None),
        ]
    ]
    async with api.sessionmaker() as session:
        original = await InterviewRepository(session, USER).get(INTERVIEW_ID)
        assert original is not None
        other_id = uuid.uuid4()
        InterviewRepository(session, USER).add(
            Interview(
                id=other_id,
                user_id=USER,
                resume_id=original.resume_id,
                jd_text="另一次面试",
                type=original.type,
                style=original.style,
                language=original.language,
                duration_min=20,
                status=status,
            )
        )
        for turn in turns:
            TurnRepository(session, USER).add(turn)
        TurnRepository(session, USER).add(
            Turn(
                id=uuid.uuid4(),
                interview_id=other_id,
                user_id=USER,
                seq=1,
                role=TurnRole.CANDIDATE,
                text="不应返回",
                start_ms=0,
                end_ms=10,
            )
        )
        await session.commit()
    response = await api.client.get(f"/api/interviews/{INTERVIEW_ID}/transcript")
    assert response.status_code == 200
    assert response.json() == {
        "interview_id": str(INTERVIEW_ID),
        "turns": [
            {
                "id": str(turn.id),
                "seq": turn.seq,
                "role": turn.role.value,
                "text": turn.text,
                "start_ms": turn.start_ms,
                "end_ms": turn.end_ms,
                "question_id": turn.question_id,
            }
            for turn in reversed(turns)
        ],
    }


@pytest.mark.parametrize("other_user", [False, True])
async def test_transcript_missing_or_other_user_is_404(api: Api, other_user: bool) -> None:
    interview_id = INTERVIEW_ID if other_user else uuid.uuid4()
    if other_user:
        api.settings.app.default_user_id = uuid.uuid4()
    response = await api.client.get(f"/api/interviews/{interview_id}/transcript")
    assert response.status_code == 404
    assert error_of(response)["message"] == "面试不存在"


@pytest.mark.parametrize(
    "status",
    [
        s
        for s in InterviewStatus
        if s
        not in {
            InterviewStatus.COMPLETED,
            InterviewStatus.ABORTED,
        }
    ],
)
async def test_transcript_requires_ended_interview(api: Api, status: InterviewStatus) -> None:
    await api.status(status)
    response = await api.client.get(f"/api/interviews/{INTERVIEW_ID}/transcript")
    assert response.status_code == 409
    assert error_of(response)["message"] == "面试结束后才可查看转写"


async def test_transcript_without_turns_is_empty(api: Api) -> None:
    response = await api.client.get(f"/api/interviews/{INTERVIEW_ID}/transcript")
    assert response.status_code == 200
    assert response.json() == {"interview_id": str(INTERVIEW_ID), "turns": []}


async def test_get_missing_or_unfinished_report(api: Api) -> None:
    await api.status(InterviewStatus.IN_PROGRESS)
    response = await api.client.get(URL)
    assert response.status_code == 404
    assert error_of(response)["code"] == "report_not_found"
    assert error_of(response)["message"] == "面试结束后才会生成报告"


@pytest.mark.parametrize("method", ["get", "post"])
async def test_report_belongs_to_current_user(api: Api, method: str) -> None:
    await api.report(ReportStatus.READY)
    api.settings.app.default_user_id = uuid.uuid4()
    response = await api.client.request(method, URL)
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


async def test_stale_pending_is_failed_without_writing_database(api: Api) -> None:
    await api.report(ReportStatus.PENDING, age_s=181)
    response = await api.client.get(URL)
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["error"] == "StaleReport"
    async with api.sessionmaker() as session:
        row = await ReportRows(session, USER).get(INTERVIEW_ID)
        assert row is not None and row.status == ReportStatus.PENDING


async def test_recent_retry_is_not_stale_despite_original_creation_time(api: Api) -> None:
    await api.report(ReportStatus.PENDING)
    async with api.sessionmaker() as session:
        row = await ReportRows(session, USER).get(INTERVIEW_ID)
        assert row is not None
        row.created_at = datetime.now(UTC) - timedelta(days=1)
        await session.commit()
    response = await api.client.get(URL)
    assert response.status_code == 200
    assert response.json()["status"] == "pending"


@pytest.mark.parametrize("previous", [None, ReportStatus.FAILED, ReportStatus.PENDING])
async def test_post_starts_report_and_persists_pending(
    api: Api, previous: ReportStatus | None
) -> None:
    if previous is not None:
        await api.report(previous, age_s=181)
    response = await api.client.post(URL)
    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert response.json()["error"] is None
    assert api.runner.starts == [(INTERVIEW_ID, USER)]
    async with api.sessionmaker() as session:
        row = await ReportRows(session, USER).get(INTERVIEW_ID)
        assert row is not None
        assert row.status == ReportStatus.PENDING
        assert row.summary_json is None
    assert (await api.client.post(URL)).status_code == 409


@pytest.mark.parametrize("status", [ReportStatus.PENDING, ReportStatus.READY])
async def test_post_does_not_replace_existing_report(api: Api, status: ReportStatus) -> None:
    await api.report(status)
    assert (await api.client.post(URL)).status_code == 409
    assert not api.runner.starts


@pytest.mark.parametrize("status", [InterviewStatus.IN_PROGRESS, InterviewStatus.FAILED])
async def test_post_requires_ended_interview(api: Api, status: InterviewStatus) -> None:
    await api.status(status)
    assert (await api.client.post(URL)).status_code == 409
    assert not api.runner.starts


async def test_post_rejects_active_runner_without_report_row(api: Api) -> None:
    api.runner.active = True
    assert (await api.client.post(URL)).status_code == 409
    assert not api.runner.starts


async def test_concurrent_post_schedules_only_one_report(api: Api) -> None:
    responses = await asyncio.gather(api.client.post(URL), api.client.post(URL))
    assert sorted(response.status_code for response in responses) == [202, 409]
    assert api.runner.starts == [(INTERVIEW_ID, USER)]


async def test_aborted_interview_can_generate_report(api: Api) -> None:
    await api.status(InterviewStatus.ABORTED)
    assert (await api.client.post(URL)).status_code == 202


async def test_resources_create_report_runner_and_close_it_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = create_resources(make_settings(user=USER))
    assert resources.reports is not None
    events: list[str] = []

    async def close_reports() -> None:
        events.append("reports")

    async def close_providers() -> None:
        events.append("providers")

    monkeypatch.setattr(resources.reports, "aclose", close_reports)
    monkeypatch.setattr(resources.providers, "aclose", close_providers)
    await resources.close()
    assert events == ["reports", "providers"]


async def test_automatic_trigger_waits_for_pending_post_registration(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    await api.status(InterviewStatus.IN_PROGRESS)
    callbacks: list[Callable[[uuid.UUID], Awaitable[None]]] = []

    async def capture_load(**kwargs: Any) -> Any:
        callbacks.append(kwargs["on_ended"])
        raise RuntimeError("callback captured")

    monkeypatch.setattr(InterviewOrchestrator, "load", capture_load)
    with pytest.raises(RuntimeError, match="callback captured"):
        await text_ws._serve(cast(Any, None), INTERVIEW_ID, USER, api.resources, "trace")

    async def automatic_trigger() -> None:
        await callbacks[0](INTERVIEW_ID)

    async with api.resources.report_requests:
        automatic = asyncio.create_task(automatic_trigger())
        await asyncio.sleep(0)
        assert not api.runner.starts
    await automatic
    assert api.runner.starts == [(INTERVIEW_ID, USER)]
