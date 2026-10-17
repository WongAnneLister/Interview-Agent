"""API 进程级资源：数据库引擎与会话工厂、Redis、厂商注册表，以及就绪检查。

lifespan 启动时创建（create_resources），关闭时释放（close）。
测试可以直接构造 Resources 注入假对象。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from interview.config import Settings
from interview.db.session import create_engine, make_sessionmaker
from interview.observability.instrument import instrument_sqlalchemy
from interview.providers.registry import ProviderRegistry
from interview.storage.base import ObjectStore
from interview.storage.s3 import S3ObjectStore

REDIS_TIMEOUT_S = 5

type ReadyCheck = Callable[[], Awaitable[object]]


@dataclass
class Resources:
    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    providers: ProviderRegistry
    ready_checks: Mapping[str, ReadyCheck] = field(default_factory=dict[str, ReadyCheck])
    engine: AsyncEngine | None = None
    redis: Redis | None = None
    storage: ObjectStore | None = None

    async def close(self) -> None:
        await self.providers.aclose()
        if self.redis is not None:
            await self.redis.aclose()
        if self.engine is not None:
            await self.engine.dispose()

    async def readiness(self) -> dict[str, str]:
        """逐项检查依赖，返回 {名称: "ok" 或错误类型}；每项的超时取配置 READY_CHECK_TIMEOUT_S。"""
        timeout_s = self.settings.app.ready_check_timeout_s

        async def run(check: ReadyCheck) -> str:
            try:
                async with asyncio.timeout(timeout_s):
                    await check()
            except TimeoutError:
                return "timeout"
            except Exception as exc:
                return type(exc).__name__
            return "ok"

        names = list(self.ready_checks)
        results = await asyncio.gather(*(run(self.ready_checks[n]) for n in names))
        return dict(zip(names, results, strict=True))


async def _ping_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


def create_resources(settings: Settings) -> Resources:
    """按配置创建资源。连接都是惰性的：这里不发起网络请求，依赖不可用时由 /readyz 反映。"""
    engine = create_engine(settings.db.url.get_secret_value())
    instrument_sqlalchemy(engine, enabled=settings.observability.enabled)
    sessionmaker = make_sessionmaker(engine)
    redis_timeout_s = max(REDIS_TIMEOUT_S, settings.app.ready_check_timeout_s)
    redis: Redis = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        settings.redis.url.get_secret_value(),
        socket_timeout=redis_timeout_s,
        socket_connect_timeout=redis_timeout_s,
    )
    providers = ProviderRegistry(settings.providers, sessionmaker=sessionmaker)

    async def ping_redis() -> object:
        return await redis.ping()  # pyright: ignore[reportUnknownMemberType]

    return Resources(
        settings=settings,
        sessionmaker=sessionmaker,
        providers=providers,
        ready_checks={"database": lambda: _ping_database(engine), "redis": ping_redis},
        engine=engine,
        redis=redis,
        storage=S3ObjectStore(settings.s3),
    )
