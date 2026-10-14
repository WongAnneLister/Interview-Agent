"""模型元数据的约定检查（不连数据库）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import ForeignKeyConstraint, Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from interview.db.models import Base, User, UserOwned

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
