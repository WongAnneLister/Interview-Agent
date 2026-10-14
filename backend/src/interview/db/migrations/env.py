"""Alembic 运行环境（异步，asyncpg）。

配置在 pyproject.toml 的 [tool.alembic]（不使用 alembic.ini）。
数据库地址优先取 config.attributes["database_url"]（测试对临时库跑迁移时传入），
否则只读取 DATABASE_URL，不加载其他配置组，跑迁移不需要 Redis、LiveKit 等配置。
"""

from __future__ import annotations

import asyncio
import logging

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection

from interview.config import DEFAULT_ENV_FILE, DatabaseSettings
from interview.db.models import Base
from interview.db.session import create_engine

config = context.config
target_metadata = Base.metadata

# 已配置过日志时（例如在测试进程里调用）basicConfig 不做任何事
logging.basicConfig(level=logging.INFO, format="%(levelname)-5.5s [%(name)s] %(message)s")


def _database_url() -> str:
    url = config.attributes.get("database_url")
    if isinstance(url, str):
        return url
    return DatabaseSettings(_env_file=DEFAULT_ENV_FILE).url.get_secret_value()  # pyright: ignore[reportCallIssue]


def run_migrations_offline() -> None:
    """只输出 SQL，不连数据库（alembic upgrade head --sql）。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
