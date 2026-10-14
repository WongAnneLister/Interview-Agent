"""在临时库上验证：迁移与模型一致、种子脚本、user_id 隔离、复合外键与级联删除。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from interview.db.models import (
    AppendOnlyError,
    Interview,
    InterviewStyle,
    InterviewType,
    Language,
    Resume,
    Turn,
    TurnRole,
    UsageEvent,
    UsageRole,
    UsageUnit,
    User,
)
from interview.db.repository import UserScopedRepository
from interview.db.scope import bind_user
from interview.db.seed import ensure_default_user
from interview.db.session import make_sessionmaker
from interview.observability.pricing import PriceTable
from interview.observability.usage import record_usage

pytestmark = pytest.mark.db


class TurnRepository(UserScopedRepository[Turn]):
    model = Turn


@dataclass
class Owner:
    user_id: uuid.UUID
    interview_id: uuid.UUID
    turn_id: uuid.UUID


def _turn(interview_id: uuid.UUID, user_id: uuid.UUID, seq: int = 0) -> Turn:
    return Turn(
        interview_id=interview_id,
        user_id=user_id,
        seq=seq,
        role=TurnRole.CANDIDATE,
        text="answer",
        start_ms=0,
        end_ms=1000,
    )


async def _make_owner(engine: AsyncEngine, email: str) -> Owner:
    """建一个用户，以及他的一份简历、一场面试和一轮发言。"""
    async with make_sessionmaker(engine)() as session:
        user = User(email=email)
        session.add(user)
        await session.flush()
        bind_user(session.sync_session, user.id)
        resume = Resume(user_id=user.id, file_key=f"resumes/{user.id}.pdf")
        session.add(resume)
        await session.flush()
        interview = Interview(
            user_id=user.id,
            resume_id=resume.id,
            jd_text="jd",
            type=InterviewType.TECHNICAL,
            style=InterviewStyle.GENTLE,
            language=Language.ZH,
            duration_min=20,
        )
        session.add(interview)
        await session.flush()
        turn = _turn(interview.id, user.id)
        session.add(turn)
        await session.commit()
        return Owner(user.id, interview.id, turn.id)


def _session_for(engine: AsyncEngine, owner: Owner) -> AsyncSession:
    session = make_sessionmaker(engine)()
    bind_user(session.sync_session, owner.user_id)
    return session


def test_migration_matches_models(migration_config: Config) -> None:
    command.check(migration_config)  # 模型有未生成迁移的改动时抛错


async def test_seed_is_idempotent_and_case_insensitive(engine: AsyncEngine) -> None:
    first = await ensure_default_user(engine)
    second = await ensure_default_user(engine, email="LOCAL@localhost")
    assert first == second
    async with make_sessionmaker(engine)() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_bound_session_only_sees_own_rows(engine: AsyncEngine) -> None:
    a = await _make_owner(engine, "a@test")
    b = await _make_owner(engine, "b@test")
    async with _session_for(engine, a) as session:
        rows = (await session.scalars(select(Turn))).all()
        assert [t.id for t in rows] == [a.turn_id]
        assert await session.get(Turn, b.turn_id) is None
        repo = TurnRepository(session, a.user_id)
        assert [t.id for t in await repo.list()] == [a.turn_id]
        assert await repo.get(b.turn_id) is None


async def test_update_and_delete_only_touch_own_rows(engine: AsyncEngine) -> None:
    a = await _make_owner(engine, "a@test")
    b = await _make_owner(engine, "b@test")
    async with _session_for(engine, a) as session:
        await session.execute(update(Turn).values(text="changed"))
        await session.execute(delete(Turn))
        await session.commit()
    async with _session_for(engine, b) as session:
        turn = await session.get(Turn, b.turn_id)
        assert turn is not None
        assert turn.text == "answer"


async def test_composite_fk_rejects_mismatched_user(engine: AsyncEngine) -> None:
    a = await _make_owner(engine, "a@test")
    b = await _make_owner(engine, "b@test")
    async with _session_for(engine, b) as session:
        # user_id 是 b（通过会话校验），但面试属于 a：数据库拒绝
        session.add(_turn(a.interview_id, b.user_id, seq=1))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_deleting_interview_cascades_to_children(engine: AsyncEngine) -> None:
    a = await _make_owner(engine, "a@test")
    async with _session_for(engine, a) as session:
        await session.execute(delete(Interview).where(Interview.id == a.interview_id))
        await session.commit()
        assert await session.get(Turn, a.turn_id) is None


async def test_usage_events_are_persisted_and_append_only(engine: AsyncEngine) -> None:
    a = await _make_owner(engine, "a@test")
    result = await record_usage(
        user_id=a.user_id,
        role=UsageRole.FRONT,
        provider="bailian",
        model="m",
        quantities={UsageUnit.INPUT_TOKENS: 1200, UsageUnit.AUDIO_SECONDS: 3.25},
        interview_id=a.interview_id,
        turn_id=a.turn_id,
        sessionmaker=make_sessionmaker(engine),
        prices=PriceTable(),
    )
    assert result.persisted
    async with _session_for(engine, a) as session:
        rows = (await session.scalars(select(UsageEvent).order_by(UsageEvent.unit))).all()
        assert [(r.unit, r.quantity, r.est_cost_cny) for r in rows] == [
            (UsageUnit.AUDIO_SECONDS, Decimal("3.25"), None),
            (UsageUnit.INPUT_TOKENS, Decimal(1200), None),
        ]
        rows[0].quantity = Decimal(0)
        with pytest.raises(AppendOnlyError):
            await session.flush()
        await session.rollback()
        await session.delete(rows[1])
        with pytest.raises(AppendOnlyError):
            await session.flush()
        await session.rollback()

        # 删除面试不连带删除用量（账本保留）
        await session.execute(delete(Interview).where(Interview.id == a.interview_id))
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(UsageEvent)) == 2
