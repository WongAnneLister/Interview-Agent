"""练习日历接口：按当地日期统计每天的回答数，只算候选人的回答，按用户隔离。"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlite_helpers import create_tables
from test_api import error_of, make_settings, open_client

from interview.api.app import create_app
from interview.api.resources import Resources
from interview.db.models import (
    Interview,
    InterviewStatus,
    InterviewStyle,
    InterviewType,
    Language,
    Plan,
    Resume,
    Turn,
    TurnRole,
    User,
)
from interview.db.session import make_sessionmaker
from interview.domain.orchestrator_store import TurnRepository
from interview.domain.plan import InterviewRepository
from interview.domain.resume import ResumeRepository
from interview.providers.registry import ProviderRegistry

USER = uuid.UUID("00000000-0000-4000-8000-000000000031")
RESUME = uuid.UUID("00000000-0000-4000-8000-000000000032")
# 北京时间 11-14 23:30 = UTC 15:30；北京时间 11-15 00:30 = UTC 16:30
LATE = datetime.now(UTC).replace(hour=15, minute=30, second=0, microsecond=0) - timedelta(days=2)


def _interview(title: str | None, status: InterviewStatus) -> Interview:
    return Interview(
        id=uuid.uuid4(),
        user_id=USER,
        resume_id=RESUME,
        jd_text="JD",
        jd_title=title,
        type=InterviewType.TECHNICAL,
        style=InterviewStyle.GENTLE,
        language=Language.ZH,
        duration_min=15,
        status=status,
    )


def _turn(interview: Interview, seq: int, role: TurnRole, at: datetime) -> Turn:
    return Turn(
        interview_id=interview.id,
        user_id=USER,
        seq=seq,
        role=role,
        text="……",
        start_ms=0,
        end_ms=0,
        created_at=at,
    )


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, Interview], object]]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await create_tables(conn, User, Resume, Interview, Plan, Turn)
    sessionmaker = make_sessionmaker(engine)
    first = _interview("AI Engineer", InterviewStatus.COMPLETED)
    second = _interview(None, InterviewStatus.IN_PROGRESS)
    async with sessionmaker() as session:
        session.add(User(id=USER, email="local@example.com"))
        await session.commit()
    async with sessionmaker() as session:
        ResumeRepository(session, USER).add(Resume(id=RESUME, user_id=USER, file_key="r.pdf"))
        interviews = InterviewRepository(session, USER)
        interviews.add(first)
        interviews.add(second)
        turns = TurnRepository(session, USER)
        turns.add(_turn(first, 1, TurnRole.INTERVIEWER, LATE))
        turns.add(_turn(first, 2, TurnRole.CANDIDATE, LATE))
        turns.add(_turn(first, 3, TurnRole.CANDIDATE, LATE + timedelta(hours=1)))
        turns.add(_turn(second, 1, TurnRole.CANDIDATE, LATE + timedelta(hours=1)))
        turns.add(_turn(second, 2, TurnRole.CANDIDATE, LATE - timedelta(days=500)))  # 一年以前
        await session.commit()

    settings = make_settings(user=USER)
    resources = Resources(
        settings=settings, sessionmaker=sessionmaker, providers=ProviderRegistry(settings.providers)
    )
    app = create_app(settings, resource_factory=lambda _s: resources, init_observability=False)
    async with open_client(app) as http:
        yield http, {"first": first, "second": second}, settings
    await engine.dispose()


async def test_answers_are_counted_per_local_day(
    client: tuple[httpx.AsyncClient, dict[str, Interview], object],
) -> None:
    http, rows, _settings = client
    day = LATE.date()
    utc = (await http.get("/api/practice/days")).json()
    assert [(d["date"], d["answers"]) for d in utc] == [(day.isoformat(), 3)]

    shanghai = (await http.get("/api/practice/days", params={"tz": "Asia/Shanghai"})).json()
    assert [(d["date"], d["answers"]) for d in shanghai] == [
        (day.isoformat(), 1),  # 23:30 那一轮还算前一天
        ((day + timedelta(days=1)).isoformat(), 2),
    ]
    sessions = {s["interview_id"]: s for s in shanghai[1]["sessions"]}
    assert sessions[str(rows["first"].id)] == {
        "interview_id": str(rows["first"].id),
        "job_title": "AI Engineer",
        "status": "completed",
        "answers": 1,
    }
    assert sessions[str(rows["second"].id)]["job_title"] == ""


async def test_other_users_see_nothing_and_bad_timezone_is_rejected(
    client: tuple[httpx.AsyncClient, dict[str, Interview], object],
) -> None:
    http, _rows, settings = client
    bad = await http.get("/api/practice/days", params={"tz": "Mars/Base"})
    assert bad.status_code == 422
    assert error_of(bad)["code"] == "invalid_timezone"
    settings.app.default_user_id = uuid.uuid4()  # type: ignore[attr-defined]
    assert (await http.get("/api/practice/days")).json() == []
