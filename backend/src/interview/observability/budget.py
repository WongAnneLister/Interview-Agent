"""预算保护：单场预算（MAX_COST_PER_INTERVIEW_CNY）和每日预算（DAILY_BUDGET_CNY）。

check() 只返回信号（ok / warn / exceeded，达到 80% 为 warn），调用方如何处理
（前台换便宜型号、提前收尾）由后续 PR 实现。

- 单场：该面试所有用量的估算成本之和，评测面试同样受限。
- 每日：北京时间当天、全部用户、不含评测（is_eval=false）的支出，属于系统级熔断。
  跨用户求和直接对表写 Core 语句，不经过会话的 user_id 拦截（见 db/scope.py），只返回一个总数。
  超预算时记错误日志（每个进程每天一次），并累加 interview_budget_exceeded_total{scope="daily"}。
- 缺价格的用量不计入成本，check() 返回 unpriced_events 提示成本被低估。
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import structlog
from sqlalchemy import Select, false, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.config import CostSettings, get_settings
from interview.db.models import UsageEvent
from interview.db.scope import bind_user
from interview.db.session import get_sessionmaker
from interview.observability.pricing import BEIJING_TZ
from interview.observability.prometheus import counter, gauge

WARN_RATIO = Decimal("0.8")

_exceeded_total = counter("budget_exceeded", "预算检查结果为超出的次数", ["scope"])
_daily_cost = gauge("daily_cost_cny", "北京时间当天全部用户、不含评测的估算支出（人民币）")
_daily_limit = gauge("daily_budget_cny", "每日预算（人民币）")
_daily_alerted: date | None = None

log = structlog.get_logger(__name__)


class BudgetStatus(enum.StrEnum):
    OK = "ok"
    WARN = "warn"
    EXCEEDED = "exceeded"


_SEVERITY = {BudgetStatus.OK: 0, BudgetStatus.WARN: 1, BudgetStatus.EXCEEDED: 2}


@dataclass(frozen=True)
class BudgetUsage:
    spent_cny: Decimal
    limit_cny: Decimal
    ratio: Decimal
    status: BudgetStatus


@dataclass(frozen=True)
class BudgetCheck:
    interview: BudgetUsage
    daily: BudgetUsage
    unpriced_events: int  # 本场缺价格的用量条数，大于 0 时成本被低估

    @property
    def status(self) -> BudgetStatus:
        """单场与每日中较严重的一个。"""
        return max(self.interview.status, self.daily.status, key=_SEVERITY.__getitem__)


def evaluate(spent: Decimal, limit: Decimal, warn_ratio: Decimal = WARN_RATIO) -> BudgetUsage:
    ratio = spent / limit
    if ratio >= 1:
        status = BudgetStatus.EXCEEDED
    elif ratio >= warn_ratio:
        status = BudgetStatus.WARN
    else:
        status = BudgetStatus.OK
    return BudgetUsage(spent, limit, ratio, status)


def beijing_day(now: datetime) -> tuple[date, datetime, datetime]:
    """now 所在的北京时间日期，以及这一天的起止时刻（UTC）。"""
    day = now.astimezone(BEIJING_TZ).date()
    start = datetime.combine(day, time(), BEIJING_TZ).astimezone(UTC)
    return day, start, start + timedelta(days=1)


def daily_spend_statement(start: datetime, end: datetime) -> Select[tuple[Decimal]]:
    """全部用户、不含评测的支出。对 Table 的 Core 语句，不受 user_id 拦截。"""
    t = UsageEvent.__table__
    return select(func.coalesce(func.sum(t.c.est_cost_cny), 0)).where(
        t.c.created_at >= start, t.c.created_at < end, t.c.is_eval == false()
    )


async def check(
    interview_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    settings: CostSettings | None = None,
    now: datetime | None = None,
) -> BudgetCheck:
    limits = settings or get_settings().cost
    day, start, end = beijing_day(now or datetime.now(UTC))
    cost = UsageEvent.est_cost_cny
    async with (sessionmaker or get_sessionmaker())() as session:
        bind_user(session.sync_session, user_id)
        spent, unpriced = (
            await session.execute(
                select(func.coalesce(func.sum(cost), 0), func.count().filter(cost.is_(None))).where(
                    UsageEvent.interview_id == interview_id
                )
            )
        ).one()
        daily_spent = await session.scalar(daily_spend_statement(start, end))

    result = BudgetCheck(
        interview=evaluate(Decimal(spent), limits.max_cost_per_interview_cny),
        daily=evaluate(Decimal(daily_spent or 0), limits.daily_budget_cny),
        unpriced_events=unpriced,
    )
    _report(result, day)
    return result


def _report(result: BudgetCheck, day: date) -> None:
    global _daily_alerted
    _daily_cost.set(float(result.daily.spent_cny))
    _daily_limit.set(float(result.daily.limit_cny))
    if result.interview.status is BudgetStatus.EXCEEDED:
        _exceeded_total.labels(scope="interview").inc()
    if result.daily.status is BudgetStatus.EXCEEDED:
        _exceeded_total.labels(scope="daily").inc()
        if _daily_alerted != day:
            _daily_alerted = day
            log.error(
                "daily_budget_exceeded",
                spent_cny=str(result.daily.spent_cny),
                limit_cny=str(result.daily.limit_cny),
            )
