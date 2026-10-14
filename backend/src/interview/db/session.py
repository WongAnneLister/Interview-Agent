"""数据库引擎与会话。

- 进程内一个引擎、一个会话工厂，第一次用到时按配置创建。
- get_session() 用于依赖注入：每次请求一个会话，由调用方显式 commit；
  未提交的事务在会话关闭时自动回滚。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine

from interview.config import get_settings
from interview.observability.instrument import instrument_sqlalchemy

# asyncpg：建立连接的超时，以及单条语句的超时（秒）
CONNECT_TIMEOUT_S = 10
COMMAND_TIMEOUT_S = 30


def create_engine(url: str, **kwargs: Any) -> AsyncEngine:
    """创建带超时设置的异步引擎；kwargs 透传给 create_async_engine（如 poolclass）。"""
    return _create_async_engine(
        url,
        pool_pre_ping=True,
        connect_args={"timeout": CONNECT_TIMEOUT_S, "command_timeout": COMMAND_TIMEOUT_S},
        **kwargs,
    )


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings = get_settings()
    engine = create_engine(settings.db.url.get_secret_value())
    instrument_sqlalchemy(engine, enabled=settings.observability.enabled)
    return engine


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    # 提交后不让对象过期，避免在异步代码里访问属性时触发隐式查询
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：yield 一个会话，请求结束时关闭。"""
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    """进程退出前关闭连接池（应用生命周期结束时调用）。"""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_sessionmaker.cache_clear()
    get_engine.cache_clear()
