"""数据库集成测试：在 DATABASE_URL 指向的 Postgres 上建临时库，跑完迁移后测试，结束时删除。

需要隧道（make tunnel）；用 make test-db 运行，make test / CI 默认不运行。
连不上数据库时直接失败，不会静默跳过。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from interview.config import DEFAULT_ENV_FILE, DatabaseSettings
from interview.db.session import create_engine

BACKEND_DIR = Path(__file__).resolve().parents[2]


def alembic_config(url: str) -> Config:
    config = Config(toml_file=str(BACKEND_DIR / "pyproject.toml"))
    config.attributes["database_url"] = url
    return config


async def _run_admin_sql(url: str, sql: str) -> None:
    # CREATE / DROP DATABASE 不能在事务里执行
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    base = make_url(
        DatabaseSettings(_env_file=DEFAULT_ENV_FILE).url.get_secret_value()  # pyright: ignore[reportCallIssue]
    )
    admin_url = base.render_as_string(hide_password=False)
    name = f"interview_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(_run_admin_sql(admin_url, f'CREATE DATABASE "{name}"'))
    try:
        url = base.set(database=name).render_as_string(hide_password=False)
        command.upgrade(alembic_config(url), "head")
        yield url
    finally:
        asyncio.run(_run_admin_sql(admin_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def migration_config(database_url: str) -> Config:
    return alembic_config(database_url)


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database_url)
    yield engine
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE users CASCADE"))
    await engine.dispose()
