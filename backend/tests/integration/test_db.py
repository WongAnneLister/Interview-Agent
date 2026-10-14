"""在临时库上验证：迁移与模型一致、种子脚本、user_id 隔离、复合外键与级联删除。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from interview.config import CostSettings
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
from interview.observability.budget import BudgetStatus, check
from interview.observability.pricing import PriceTable
from interview.observability.usage import cost_breakdown, record_usage

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


def _usage(
    owner: Owner,
    cost: str | None,
    *,
    role: UsageRole = UsageRole.FRONT,
    provider: str = "bailian",
    is_eval: bool = False,
    created_at: datetime | None = None,
    interview: bool = True,
) -> UsageEvent:
    return UsageEvent(
        user_id=owner.user_id,
        interview_id=owner.interview_id if interview else None,
        role=role,
        provider=provider,
        model="m",
        unit=UsageUnit.INPUT_TOKENS,
        quantity=Decimal(1000),
        est_cost_cny=None if cost is None else Decimal(cost),
        is_eval=is_eval,
        created_at=created_at or datetime.now(UTC),
    )


async def _add(engine: AsyncEngine, owner: Owner, *events: UsageEvent) -> None:
    async with _session_for(engine, owner) as session:
        session.add_all(events)
        await session.commit()


async def test_budget_check_interview_and_daily(engine: AsyncEngine) -> None:
    a = await _make_owner(engine, "a@test")
    b = await _make_owner(engine, "b@test")
    yesterday = datetime.now(UTC) - timedelta(days=2)
    await _add(engine, a, _usage(a, "3"), _usage(a, "1.2"), _usage(a, None))
    await _add(
        engine,
        b,
        _usage(b, "30", interview=False),
        _usage(b, "100", is_eval=True),  # 评测不计入每日预算
        _usage(b, "500", created_at=yesterday),  # 不是今天
    )
    result = await check(
        a.interview_id,
        user_id=a.user_id,
        sessionmaker=make_sessionmaker(engine),
        settings=CostSettings(max_cost_per_interview_cny=Decimal(5), daily_budget_cny=Decimal(40)),
    )
    assert result.interview.spent_cny == Decimal("4.2")
    assert result.interview.status is BudgetStatus.WARN
    assert result.unpriced_events == 1
    # 全部用户、不含评测：a 的 4.2 + b 的 30
    assert result.daily.spent_cny == Decimal("34.2")
    assert result.daily.status is BudgetStatus.WARN

    # b 的面试单场已超（评测面试同样受单场预算限制）
    result_b = await check(
        b.interview_id,
        user_id=b.user_id,
        sessionmaker=make_sessionmaker(engine),
        settings=CostSettings(max_cost_per_interview_cny=Decimal(5), daily_budget_cny=Decimal(30)),
    )
    assert result_b.interview.status is BudgetStatus.EXCEEDED
    assert result_b.daily.status is BudgetStatus.EXCEEDED
    assert result_b.status is BudgetStatus.EXCEEDED


async def test_cost_breakdown_for_one_interview(engine: AsyncEngine) -> None:
    a = await _make_owner(engine, "a@test")
    b = await _make_owner(engine, "b@test")
    async with _session_for(engine, a) as session:
        interview = await session.get(Interview, a.interview_id)
        assert interview is not None
        interview.started_at = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
        interview.ended_at = datetime(2026, 10, 14, 10, 25, tzinfo=UTC)
        await session.commit()
    await _add(
        engine,
        a,
        _usage(a, "1"),
        _usage(a, "0.5"),
        _usage(a, "0.5", role=UsageRole.TTS),
        _usage(a, None, role=UsageRole.ASR),
        _usage(a, "0.5", role=UsageRole.FRONT, provider="deepseek"),
    )
    await _add(engine, b, _usage(b, "99"))

    breakdown = await cost_breakdown(
        a.interview_id, user_id=a.user_id, sessionmaker=make_sessionmaker(engine)
    )
    assert breakdown.total_cny == Decimal("2.5")
    assert breakdown.by_role == {"front": Decimal("2"), "tts": Decimal("0.5"), "asr": 0}
    assert breakdown.by_provider == {"bailian": Decimal("2"), "deepseek": Decimal("0.5")}
    assert breakdown.duration_min == Decimal("25.00")
    assert breakdown.cost_per_min_cny == Decimal("0.1")
    assert breakdown.unpriced_events == 1
    front = next(x for x in breakdown.lines if x.role == "front" and x.provider == "bailian")
    assert (front.cost_cny, front.events) == (Decimal("1.5"), 2)
