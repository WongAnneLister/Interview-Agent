"""汇总本次评测的用量和估算费用：评测用户自 run 开始以来带 is_eval 的全部用量。"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.db.models import UsageEvent
from interview.db.scope import bind_user
from interview.observability.usage import CostBreakdown, CostLine, summarize


async def run_cost(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID, since: datetime
) -> CostBreakdown:
    cost = UsageEvent.est_cost_cny
    statement = (
        select(
            UsageEvent.role,
            UsageEvent.provider,
            UsageEvent.model,
            func.coalesce(func.sum(cost), 0),
            func.count(),
            func.count().filter(cost.is_(None)),
        )
        .where(
            UsageEvent.user_id == user_id,
            UsageEvent.is_eval.is_(True),
            UsageEvent.created_at >= since,
        )
        .group_by(UsageEvent.role, UsageEvent.provider, UsageEvent.model)
    )
    async with sm() as session:
        bind_user(session.sync_session, user_id)
        rows = (await session.execute(statement)).all()
    lines = [
        CostLine(role.value, provider, model, Decimal(total), events, unpriced)
        for role, provider, model, total, events, unpriced in rows
    ]
    return summarize(lines, None)
