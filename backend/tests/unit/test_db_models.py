"""模型元数据的约定检查（不连数据库）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import ForeignKeyConstraint, Table, delete, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from interview.db.models import AppendOnlyError, Base, UsageEvent, User, UserOwned
from interview.db.session import create_engine, make_sessionmaker

BACKEND_DIR = Path(__file__).resolve().parents[2]
TABLES = Base.metadata.tables
BUSINESS_TABLES = [t for name, t in TABLES.items() if name != "users"]
INTERVIEW_CHILDREN = ["plans", "turns", "evaluations", "reports", "turn_metrics"]


def test_all_expected_tables_exist() -> None:
    assert set(TABLES) == {
        "users",
        "resumes",
        "interviews",
        "plans",
        "turns",
        "evaluations",
        "reports",
        "turn_metrics",
        "usage_events",
    }


@pytest.mark.parametrize("table", BUSINESS_TABLES, ids=lambda t: t.name)
def test_business_tables_have_non_null_user_id(table: Table) -> None:
    assert "user_id" in table.c
    assert not table.c.user_id.nullable


def test_business_models_are_marked_user_owned() -> None:
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        assert issubclass(cls, UserOwned) == (cls is not User), cls.__name__


@pytest.mark.parametrize("name", INTERVIEW_CHILDREN)
def test_interview_children_use_composite_fk(name: str) -> None:
    fks = [c for c in TABLES[name].constraints if isinstance(c, ForeignKeyConstraint)]
    composite = [fk for fk in fks if fk.referred_table.name == "interviews"]
    assert len(composite) == 1
    assert composite[0].column_keys == ["interview_id", "user_id"]
    assert composite[0].ondelete == "CASCADE"


def test_timestamps_are_timezone_aware() -> None:
    for table in TABLES.values():
        for col in table.c:
            if col.name.endswith("_at"):
                assert getattr(col.type, "timezone", False), f"{table.name}.{col.name}"


def test_enums_are_stored_as_values_with_check_constraint() -> None:
    ddl = str(CreateTable(TABLES["interviews"]).compile(dialect=postgresql.dialect()))
    assert "status IN ('draft', 'planned', 'in_progress'" in ddl
    assert "CONSTRAINT ck_interviews_interview_status" in ddl


def test_migrations_have_single_head() -> None:
    config = Config(toml_file=str(BACKEND_DIR / "pyproject.toml"))
    assert len(ScriptDirectory.from_config(config).get_heads()) == 1


def test_report_prompt_version_fits_both_prompt_versions() -> None:
    version = "report_question@1+report_summary@1"
    assert getattr(TABLES["reports"].c.prompt_version.type, "length", 0) >= len(version)


def test_usage_events_have_no_interview_or_turn_fk() -> None:
    # 只追加的账本：删除面试或发言时用量记录保留
    fks = [c for c in TABLES["usage_events"].constraints if isinstance(c, ForeignKeyConstraint)]
    assert [fk.referred_table.name for fk in fks] == ["users"]


async def test_usage_events_reject_bulk_update_and_delete() -> None:
    # 拦截发生在建立连接之前，不需要数据库
    engine = create_engine("postgresql+asyncpg://u:p@127.0.0.1:1/db")
    async with make_sessionmaker(engine)() as session:
        with pytest.raises(AppendOnlyError):
            await session.execute(update(UsageEvent).values(quantity=0))
        with pytest.raises(AppendOnlyError):
            await session.execute(delete(UsageEvent))
    await engine.dispose()
