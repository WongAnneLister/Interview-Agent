"""SQLite 单元测试的建表辅助。

PostgreSQL 的 JSONB 和 gen_random_uuid() 在 SQLite 里不存在：
- 编译 DDL 时把 JSONB 映射为 JSON；
- 建表时暂时去掉 UUID 主键的 gen_random_uuid() 服务端默认值。
@compiles 注册是进程级的，集中在这里避免多个测试模块重复注册。
"""

from __future__ import annotations

from functools import partial
from typing import Any, cast

from sqlalchemy import Connection
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateTable


@compiles(JSONB, "sqlite")
def _jsonb_as_json(_type: JSONB, _compiler: object, **_kw: object) -> str:
    return "JSON"


@compiles(CreateTable, "sqlite")
def _strip_pg_uuid_default(element: CreateTable, compiler: Any, **kw: Any) -> str:
    saved: list[tuple[object, object]] = []
    for col in element.element.columns:
        default = col.server_default
        if default is not None and "gen_random_uuid" in str(getattr(default, "arg", "")):
            saved.append((col, default))
            col.server_default = None
    try:
        visit = compiler.visit_create_table
        return str(visit(element, **kw))
    finally:
        for col, default in saved:
            col.server_default = default  # type: ignore[attr-defined]


def _create(sync: Connection, model: Any) -> None:
    cast(Any, model.__table__).create(sync)


async def create_tables(conn: AsyncConnection, *models: Any) -> None:
    """按模型建表（SQLite）。调用方按外键顺序传入模型即可。"""
    for model in models:
        await conn.run_sync(partial(_create, model=model))
