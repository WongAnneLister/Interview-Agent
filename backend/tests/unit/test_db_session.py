"""引擎与会话工厂（不连数据库：asyncpg 只在第一次执行语句时才建立连接）。"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from interview.db import session as db_session

URL = "postgresql+asyncpg://u:p@127.0.0.1:1/db"


@pytest.fixture(autouse=True)
def _fresh_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    settings = SimpleNamespace(
        db=SimpleNamespace(url=SecretStr(URL)), observability=SimpleNamespace(enabled=False)
    )
    monkeypatch.setattr(db_session, "get_settings", lambda: settings)
    db_session.get_engine.cache_clear()
    db_session.get_sessionmaker.cache_clear()
    yield
    db_session.get_engine.cache_clear()
    db_session.get_sessionmaker.cache_clear()


def test_engine_uses_asyncpg_with_pre_ping() -> None:
    engine = db_session.create_engine(URL)
    assert engine.pool._pre_ping  # pyright: ignore[reportAttributeAccessIssue]
    assert engine.dialect.name == "postgresql"
    assert engine.dialect.driver == "asyncpg"


def test_engine_and_sessionmaker_are_process_singletons() -> None:
    assert db_session.get_engine() is db_session.get_engine()
    assert db_session.get_sessionmaker() is db_session.get_sessionmaker()


async def test_get_session_yields_and_closes_session() -> None:
    gen = db_session.get_session()
    session = await anext(gen)
    assert isinstance(session, AsyncSession)
    assert session.bind is db_session.get_engine()
    with pytest.raises(StopAsyncIteration):
        await anext(gen)


async def test_dispose_engine_resets_singletons() -> None:
    first = db_session.get_engine()
    await db_session.dispose_engine()
    assert db_session.get_engine() is not first
