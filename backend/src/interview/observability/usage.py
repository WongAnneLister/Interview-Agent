"""用量记录：每次厂商调用调用一次 record_usage()，把各计量单位的用量一起传入。

做三件事：
1. 写入只追加的 usage_events 表（每个单位一行）。用独立的短会话立即提交，
   与业务事务解耦：业务回滚或结构化输出重试时，已经发生的费用不会丢。
2. 把用量和成本写到 span（Langfuse 的 usage_details / cost_details），Langfuse 直接显示成本。
   Langfuse 的成本没有币种，界面显示 “$”，实际数值是人民币（metadata 标注 cost_currency=CNY）。
3. 累加 Prometheus 计数器 interview_cost_cny_total、interview_tokens_total。

cost_breakdown() 按角色、厂商汇总一场面试的成本，并给出每分钟成本（tools/cost_report.py 使用）。
缺价格时成本记为空（见 pricing.py）；写库失败只记错误日志和计数器，不抛出，不中断面试。
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from opentelemetry import trace
from opentelemetry.trace import Span, format_trace_id
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.db.models import Interview, UsageEvent, UsageRole, UsageUnit
from interview.db.scope import bind_user
from interview.db.session import get_sessionmaker
from interview.observability.pricing import COST_QUANTUM, PriceTable, get_price_table
from interview.observability.prometheus import counter
from interview.observability.tracing import LF

TOKEN_UNITS = frozenset(
    {
        UsageUnit.INPUT_TOKENS,
        UsageUnit.OUTPUT_TOKENS,
        UsageUnit.CACHED_TOKENS,
        UsageUnit.REASONING_TOKENS,
    }
)

# Langfuse usage_details / cost_details 的键（与 Langfuse 对 OpenAI 用量的命名一致）
LANGFUSE_KEYS: Mapping[UsageUnit, str] = {
    UsageUnit.INPUT_TOKENS: "input",
    UsageUnit.CACHED_TOKENS: "input_cached_tokens",
    UsageUnit.OUTPUT_TOKENS: "output",
    UsageUnit.REASONING_TOKENS: "output_reasoning_tokens",
    UsageUnit.AUDIO_SECONDS: "audio_seconds",
    UsageUnit.CHARACTERS: "characters",
}

_LABELS = ["role", "provider", "model", "is_eval"]
_cost_total = counter("cost_cny", "估算的厂商调用成本（人民币，缺价格的不计入）", _LABELS)
_tokens_total = counter("tokens", "厂商调用的 token 数", [*_LABELS, "unit"])
_record_failures = counter("usage_record_failures", "用量写入数据库失败的次数", ["role"])

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class UsageResult:
    events: list[UsageEvent]  # 每个非零单位一条
    total_cost_cny: Decimal | None  # 任一单位缺价格时为空
    unpriced_units: tuple[UsageUnit, ...]
    persisted: bool  # 是否已写入数据库


def llm_quantities(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> dict[UsageUnit, int]:
    """把厂商返回的 token 数拆成互不重叠的计量单位。

    厂商的 prompt_tokens 通常包含命中缓存的部分、completion_tokens 包含推理部分；
    口径不同的厂商由适配器换算成这种口径再调用。
    """
    cached = min(cached_tokens, prompt_tokens)
    reasoning = min(reasoning_tokens, completion_tokens)
    return {
        UsageUnit.INPUT_TOKENS: prompt_tokens - cached,
        UsageUnit.CACHED_TOKENS: cached,
        UsageUnit.OUTPUT_TOKENS: completion_tokens - reasoning,
        UsageUnit.REASONING_TOKENS: reasoning,
    }


async def record_usage(
    *,
    user_id: uuid.UUID,
    role: UsageRole,
    provider: str,
    model: str,
    quantities: Mapping[UsageUnit, int | float | Decimal],
    interview_id: uuid.UUID | None = None,
    turn_id: uuid.UUID | None = None,
    is_eval: bool = False,
    is_retry: bool = False,
    is_fallback: bool = False,
    span: Span | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    prices: PriceTable | None = None,
) -> UsageResult:
    """记录一次厂商调用的用量。span 默认取当前 span；数量为 0 的单位不写入。"""
    target = span or trace.get_current_span()
    table = prices or get_price_table()
    at = datetime.now(UTC)
    ctx = target.get_span_context()
    trace_id = format_trace_id(ctx.trace_id) if ctx.is_valid else None

    events: list[UsageEvent] = []
    unpriced: list[UsageUnit] = []
    for unit, raw in quantities.items():
        quantity = Decimal(str(raw))
        if quantity < 0:
            raise ValueError(f"用量不能为负数：{unit.value}={raw}")
        if quantity == 0:
            continue
        cost = table.cost(provider, model, unit, quantity, at)
        if cost is None:
            unpriced.append(unit)
        events.append(
            UsageEvent(
                id=uuid.uuid4(),
                user_id=user_id,
                interview_id=interview_id,
                turn_id=turn_id,
                trace_id=trace_id,
                role=role,
                provider=provider,
                model=model,
                unit=unit,
                quantity=quantity,
                est_cost_cny=cost,
                is_eval=is_eval,
                is_retry=is_retry,
                is_fallback=is_fallback,
                created_at=at,
            )
        )

    priced = [e.est_cost_cny for e in events if e.est_cost_cny is not None]
    total = None if unpriced else sum(priced, Decimal(0))
    _annotate_span(target, events, unpriced)
    labels = {
        "role": role.value,
        "provider": provider,
        "model": model,
        "is_eval": str(is_eval).lower(),
    }
    if priced:
        _cost_total.labels(**labels).inc(float(sum(priced, Decimal(0))))
    for e in events:
        if e.unit in TOKEN_UNITS:
            _tokens_total.labels(**labels, unit=e.unit.value).inc(float(e.quantity))

    persisted = not events or await _persist(
        sessionmaker or get_sessionmaker(), user_id, role, events
    )
    return UsageResult(events, total, tuple(unpriced), persisted)


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _annotate_span(target: Span, events: list[UsageEvent], unpriced: list[UsageUnit]) -> None:
    usage = {LANGFUSE_KEYS[e.unit]: _number(e.quantity) for e in events}
    costs = {
        LANGFUSE_KEYS[e.unit]: float(e.est_cost_cny) for e in events if e.est_cost_cny is not None
    }
    costs["total"] = sum(costs.values())
    target.set_attribute(LF.USAGE_DETAILS, json.dumps(usage))
    target.set_attribute(LF.COST_DETAILS, json.dumps(costs))
    target.set_attribute(LF.METADATA + "cost_currency", "CNY")
    if unpriced:
        target.set_attribute(LF.METADATA + "unpriced_units", ",".join(u.value for u in unpriced))


async def _persist(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    role: UsageRole,
    events: list[UsageEvent],
) -> bool:
    try:
        async with sessionmaker() as session:
            bind_user(session.sync_session, user_id)
            session.add_all(events)
            await session.commit()
    except Exception:
        _record_failures.labels(role=role.value).inc()
        log.exception("usage_record_failed", role=role.value, events=len(events))
        return False
    return True


@dataclass(frozen=True)
class CostLine:
    """一场面试里某个 角色 + 厂商 + 型号 的成本合计。"""

    role: str
    provider: str
    model: str
    cost_cny: Decimal  # 缺价格的记录不计入
    events: int
    unpriced_events: int


@dataclass(frozen=True)
class CostBreakdown:
    total_cny: Decimal
    by_role: dict[str, Decimal]
    by_provider: dict[str, Decimal]
    lines: list[CostLine]
    duration_min: Decimal | None  # 面试未开始或未结束时为空
    cost_per_min_cny: Decimal | None
    unpriced_events: int  # 大于 0 时成本被低估


def summarize(lines: Iterable[CostLine], duration_s: float | None) -> CostBreakdown:
    """把明细汇总为按角色、按厂商的成本和每分钟成本。"""
    items = sorted(lines, key=lambda line: line.cost_cny, reverse=True)
    by_role: defaultdict[str, Decimal] = defaultdict(Decimal)
    by_provider: defaultdict[str, Decimal] = defaultdict(Decimal)
    for line in items:
        by_role[line.role] += line.cost_cny
        by_provider[line.provider] += line.cost_cny
    total = sum((line.cost_cny for line in items), Decimal(0))
    minutes = Decimal(str(duration_s)) / 60 if duration_s and duration_s > 0 else None
    return CostBreakdown(
        total_cny=total,
        by_role=dict(by_role),
        by_provider=dict(by_provider),
        lines=items,
        duration_min=minutes.quantize(Decimal("0.01")) if minutes else None,
        cost_per_min_cny=(total / minutes).quantize(COST_QUANTUM) if minutes else None,
        unpriced_events=sum(line.unpriced_events for line in items),
    )


async def cost_breakdown(
    interview_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> CostBreakdown:
    """一场面试的成本拆解；时长取 interviews.ended_at - started_at。"""
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
        .where(UsageEvent.interview_id == interview_id)
        .group_by(UsageEvent.role, UsageEvent.provider, UsageEvent.model)
    )
    async with (sessionmaker or get_sessionmaker())() as session:
        bind_user(session.sync_session, user_id)
        rows = (await session.execute(statement)).all()
        times = (
            await session.execute(
                select(Interview.started_at, Interview.ended_at).where(Interview.id == interview_id)
            )
        ).one_or_none()
    duration_s = None
    if times is not None and times.started_at is not None and times.ended_at is not None:
        duration_s = (times.ended_at - times.started_at).total_seconds()
    lines = [
        CostLine(role.value, provider, model, Decimal(total), events, unpriced)
        for role, provider, model, total, events, unpriced in rows
    ]
    return summarize(lines, duration_s)
