"""用量记录：每次厂商调用调用一次 record_usage()，把各计量单位的用量一起传入。

做三件事：
1. 写入只追加的 usage_events 表（每个单位一行）。用独立的短会话立即提交，
   与业务事务解耦：业务回滚或结构化输出重试时，已经发生的费用不会丢。
2. 把用量和成本写到 span（Langfuse 的 usage_details / cost_details），Langfuse 直接显示成本。
   Langfuse 的成本没有币种，界面显示 “$”，实际数值是人民币（metadata 标注 cost_currency=CNY）。
3. 累加 Prometheus 计数器 interview_cost_cny_total、interview_tokens_total。

缺价格时成本记为空（见 pricing.py）；写库失败只记错误日志和计数器，不抛出，不中断面试。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from opentelemetry import trace
from opentelemetry.trace import Span, format_trace_id
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.db.models import UsageEvent, UsageRole, UsageUnit
from interview.db.scope import bind_user
from interview.db.session import get_sessionmaker
from interview.observability.pricing import PriceTable, get_price_table
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
