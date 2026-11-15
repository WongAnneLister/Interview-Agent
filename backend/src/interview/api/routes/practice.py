"""练习日历：最近一年里每天答了几轮、练了哪几场，前端画成类似 GitHub 贡献图的格子。

“练习”按候选人的回答算：哪天在面试里作答过，哪天就算练过；只建了面试没开始的不算。
按调用方传来的时区（浏览器的 IANA 时区名）把回答时间分到当地日期，深夜练习不会算到第二天。
只读，按 user_id 限定。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from interview.api.deps import CurrentUserId, DbSession
from interview.api.errors import AppError
from interview.db.models import Interview, InterviewStatus, Turn, TurnRole
from interview.domain.plan import InterviewRepository

router = APIRouter(prefix="/api/practice", tags=["practice"])

# 日历画 53 周，多取几天保证第一列完整
MAX_DAYS = 400


class PracticeSession(BaseModel):
    interview_id: uuid.UUID
    job_title: str
    status: InterviewStatus
    answers: int  # 这一天在这场面试里答了几轮


class PracticeDay(BaseModel):
    date: date
    answers: int
    sessions: list[PracticeSession]


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AppError("不认识的时区", code="invalid_timezone", status_code=422) from exc


def _utc(moment: datetime) -> datetime:
    # SQLite 测试库读回来没有时区；生产的 PostgreSQL 是 timestamptz
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@router.get("/days")
async def practice_days(
    user_id: CurrentUserId,
    session: DbSession,
    tz: Annotated[str, Query(max_length=64, description="浏览器的 IANA 时区名")] = "UTC",
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = 371,
) -> list[PracticeDay]:
    """有练习的日子，按日期升序；没练的日子不返回。"""
    zone = _zone(tz)
    since = datetime.now(UTC) - timedelta(days=days)
    interviews = InterviewRepository(session, user_id)
    query = (
        select(Turn.interview_id, Turn.created_at)
        .where(
            Turn.user_id == user_id,
            Turn.role == TurnRole.CANDIDATE,
            Turn.created_at >= since,
        )
        .order_by(Turn.created_at)
    )
    counts: dict[date, dict[uuid.UUID, int]] = defaultdict(lambda: defaultdict(int))
    for interview_id, created_at in (await session.execute(query)).tuples():
        counts[_utc(created_at).astimezone(zone).date()][interview_id] += 1
    if not counts:
        return []

    ids = {interview_id for per_day in counts.values() for interview_id in per_day}
    rows = await interviews.list(Interview.id.in_(ids))
    info = {row.id: (row.jd_title or "", row.status) for row in rows}
    result: list[PracticeDay] = []
    for day in sorted(counts):
        sessions = [
            PracticeSession(
                interview_id=interview_id,
                job_title=info[interview_id][0],
                status=info[interview_id][1],
                answers=answers,
            )
            for interview_id, answers in counts[day].items()
            if interview_id in info
        ]
        if sessions:
            result.append(
                PracticeDay(date=day, answers=sum(s.answers for s in sessions), sessions=sessions)
            )
    return result
