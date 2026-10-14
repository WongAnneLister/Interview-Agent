"""预算三种状态与边界、北京时间日界、每日支出不含评测且跨用户、超预算告警；成本汇总。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from interview.db.models import UsageEvent
from interview.db.scope import MissingUserScopeError
from interview.db.session import create_engine, make_sessionmaker
from interview.observability import budget
from interview.observability.budget import (
    BudgetCheck,
    BudgetStatus,
    BudgetUsage,
    beijing_day,
    daily_spend_statement,
    evaluate,
)
from interview.observability.usage import CostLine, summarize


def usage(status: BudgetStatus, spent: str = "1", limit: str = "5") -> BudgetUsage:
    return BudgetUsage(Decimal(spent), Decimal(limit), Decimal(spent) / Decimal(limit), status)


@pytest.mark.parametrize(
    ("spent", "status"),
    [
        ("0", BudgetStatus.OK),
        ("3.99", BudgetStatus.OK),
        ("4", BudgetStatus.WARN),  # 正好 80%
        ("4.99999999", BudgetStatus.WARN),
        ("5", BudgetStatus.EXCEEDED),  # 正好 100%
        ("7.5", BudgetStatus.EXCEEDED),
    ],
)
def test_evaluate_three_states(spent: str, status: BudgetStatus) -> None:
    result = evaluate(Decimal(spent), Decimal(5))
    assert result.status is status
    assert result.ratio == Decimal(spent) / 5


def test_overall_status_is_the_worse_one() -> None:
    ok, warn, exceeded = BudgetStatus.OK, BudgetStatus.WARN, BudgetStatus.EXCEEDED
    assert BudgetCheck(usage(ok), usage(ok), 0).status is ok
    assert BudgetCheck(usage(warn), usage(ok), 0).status is warn
    assert BudgetCheck(usage(ok), usage(exceeded), 0).status is exceeded
    assert BudgetCheck(usage(exceeded), usage(warn), 0).status is exceeded


def test_beijing_day_window() -> None:
    day, start, end = beijing_day(datetime(2026, 10, 14, 16, 30, tzinfo=UTC))
    assert day == date(2026, 10, 15)  # 北京时间已是 15 日 00:30
    assert start == datetime(2026, 10, 14, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 15, 16, 0, tzinfo=UTC)


def test_daily_spend_excludes_eval_and_is_not_user_scoped() -> None:
    _, start, end = beijing_day(datetime.now(UTC))
    sql = str(daily_spend_statement(start, end).compile(dialect=postgresql.dialect()))
    assert "usage_events.is_eval = false" in sql
    assert "user_id" not in sql


@pytest.fixture
async def unbound_session() -> AsyncIterator[AsyncSession]:
    engine = create_engine("postgresql+asyncpg://u:p@127.0.0.1:1/db")
    async with make_sessionmaker(engine)() as s:
        yield s
    await engine.dispose()


async def test_daily_statement_bypasses_scope_but_orm_query_does_not(
    unbound_session: AsyncSession,
) -> None:
    # ORM 查询在未绑定用户时被拦截；每日总额的 Core 语句不经过拦截（这里因连不上数据库而失败）
    with pytest.raises(MissingUserScopeError):
        await unbound_session.execute(select(UsageEvent))
    _, start, end = beijing_day(datetime.now(UTC))
    with pytest.raises(OSError):
        await unbound_session.execute(daily_spend_statement(start, end))


def test_daily_exceeded_logs_once_per_day_and_counts_every_time() -> None:
    def sample(scope: str) -> float:
        return REGISTRY.get_sample_value("interview_budget_exceeded_total", {"scope": scope}) or 0

    result = BudgetCheck(
        usage(BudgetStatus.EXCEEDED, "6"), usage(BudgetStatus.EXCEEDED, "60", "50"), 0
    )
    before_daily, before_interview = sample("daily"), sample("interview")
    with capture_logs() as logs:
        budget._report(result, date(2030, 1, 1))  # pyright: ignore[reportPrivateUsage]
        budget._report(result, date(2030, 1, 1))  # pyright: ignore[reportPrivateUsage]
        budget._report(result, date(2030, 1, 2))  # pyright: ignore[reportPrivateUsage]
    errors = [e for e in logs if e["event"] == "daily_budget_exceeded"]
    assert len(errors) == 2  # 每天一次
    assert errors[0]["log_level"] == "error"
    assert sample("daily") == before_daily + 3
    assert sample("interview") == before_interview + 3
    assert REGISTRY.get_sample_value("interview_daily_cost_cny") == 60
    assert REGISTRY.get_sample_value("interview_daily_budget_cny") == 50


def line(role: str, provider: str, cost: str, unpriced: int = 0) -> CostLine:
    return CostLine(role, provider, f"{provider}-model", Decimal(cost), 3, unpriced)


def test_summarize_groups_by_role_and_provider() -> None:
    b = summarize(
        [
            line("front", "bailian", "0.5"),
            line("front", "deepseek", "0.2"),
            line("eval", "bailian", "1.1", unpriced=2),
            line("tts", "bailian", "0.6"),
        ],
        duration_s=20 * 60,
    )
    assert b.total_cny == Decimal("2.4")
    assert b.by_role == {"eval": Decimal("1.1"), "front": Decimal("0.7"), "tts": Decimal("0.6")}
    assert b.by_provider == {"bailian": Decimal("2.2"), "deepseek": Decimal("0.2")}
    assert [x.cost_cny for x in b.lines] == sorted((x.cost_cny for x in b.lines), reverse=True)
    assert b.duration_min == Decimal("20.00")
    assert b.cost_per_min_cny == Decimal("0.12")
    assert b.unpriced_events == 2


@pytest.mark.parametrize("duration_s", [None, 0])
def test_summarize_without_duration(duration_s: float | None) -> None:
    b = summarize([line("front", "bailian", "1")], duration_s)
    assert b.duration_min is None
    assert b.cost_per_min_cny is None


def test_summarize_empty() -> None:
    b = summarize([], 600)
    assert (b.total_cny, b.by_role, b.lines, b.unpriced_events) == (0, {}, [], 0)
    assert b.cost_per_min_cny == 0
