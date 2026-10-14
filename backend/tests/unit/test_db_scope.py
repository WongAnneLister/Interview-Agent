"""user_id 隔离：仓储基类与会话级拦截（不连数据库，拦截在建立连接之前就会抛错）。"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.base import Executable

from interview.db.models import Turn, TurnRole
from interview.db.repository import UserScopedRepository
from interview.db.scope import (
    MissingUserScopeError,
    UserScopeMismatchError,
    bind_user,
    bound_user,
)
from interview.db.session import create_engine, make_sessionmaker

# 不可达地址：如果拦截没生效，测试会以连接错误失败，而不是静默通过
URL = "postgresql+asyncpg://u:p@127.0.0.1:1/db"


class TurnRepository(UserScopedRepository[Turn]):
    model = Turn


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine(URL)
    async with make_sessionmaker(engine)() as s:
        yield s
    await engine.dispose()


def _turn(user_id: uuid.UUID) -> Turn:
    return Turn(
        interview_id=uuid.uuid4(),
        user_id=user_id,
        seq=0,
        role=TurnRole.CANDIDATE,
        text="x",
        start_ms=0,
        end_ms=1,
    )


async def test_repository_without_user_id_raises(session: AsyncSession) -> None:
    with pytest.raises(MissingUserScopeError):
        TurnRepository(session, None)


@pytest.mark.parametrize(
    "stmt",
    [select(Turn), update(Turn).values(text="y"), delete(Turn)],
    ids=["select", "update", "delete"],
)
async def test_query_on_unbound_session_raises(session: AsyncSession, stmt: Executable) -> None:
    with pytest.raises(MissingUserScopeError):
        await session.execute(stmt)


async def test_get_on_unbound_session_raises(session: AsyncSession) -> None:
    with pytest.raises(MissingUserScopeError):
        await session.get(Turn, uuid.uuid4())


async def test_flush_on_unbound_session_raises(session: AsyncSession) -> None:
    session.add(_turn(uuid.uuid4()))
    with pytest.raises(MissingUserScopeError):
        await session.flush()


async def test_flush_with_other_users_object_raises(session: AsyncSession) -> None:
    bind_user(session.sync_session, uuid.uuid4())
    session.add(_turn(uuid.uuid4()))
    with pytest.raises(UserScopeMismatchError):
        await session.flush()


async def test_session_cannot_be_rebound_to_another_user(session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    TurnRepository(session, user_id)
    TurnRepository(session, user_id)  # 同一用户可以重复绑定
    assert bound_user(session.sync_session) == user_id
    with pytest.raises(UserScopeMismatchError):
        TurnRepository(session, uuid.uuid4())


async def test_repository_rejects_other_users_object(session: AsyncSession) -> None:
    repo = TurnRepository(session, uuid.uuid4())
    with pytest.raises(UserScopeMismatchError):
        repo.add(_turn(uuid.uuid4()))
    with pytest.raises(UserScopeMismatchError):
        await repo.delete(_turn(uuid.uuid4()))


async def test_repository_select_filters_by_user_id(session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    sql = TurnRepository(session, user_id).select().compile()
    assert "turns.user_id = :user_id_1" in str(sql)
    assert sql.params["user_id_1"] == user_id
