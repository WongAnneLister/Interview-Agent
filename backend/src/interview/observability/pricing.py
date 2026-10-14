"""厂商单价表：加载并校验 config/pricing.yaml，按“厂商 + 型号 + 计量单位 + 生效日期”估算成本。

- 单价表格式错误（未知单位、负价、重复条目等）属于配置错误，加载时直接抛 PricingError。
- 缺价格（没有条目、单位未列出或 price_cny 为空）不抛异常：成本返回 None，
  每个进程对同一 厂商/型号/单位 只告警一次，并累加 interview_pricing_missing_total。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from interview.config import get_settings
from interview.db.models import UsageUnit
from interview.observability.prometheus import counter

# 生效日期按北京时间（无夏令时，用固定偏移，不依赖系统时区库）
BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
# 与 usage_events.est_cost_cny 的精度一致
COST_QUANTUM = Decimal("0.00000001")

log = structlog.get_logger(__name__)

_missing_total = counter(
    "pricing_missing",
    "估算成本时缺少单价的用量条数",
    ["provider", "model", "unit"],
)
_warned: set[tuple[str, str, str]] = set()


class PricingError(ValueError):
    """单价表文件不存在或格式不正确。"""


class UnitPrice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    price_cny: Annotated[Decimal, Field(ge=0)] | None
    per: int = Field(default=1, gt=0)


class PriceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    effective_from: date
    source: str | None = None
    units: dict[UsageUnit, UnitPrice]


class PricingFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prices: list[PriceEntry] = Field(default_factory=list[PriceEntry])

    @model_validator(mode="after")
    def _no_duplicates(self) -> PricingFile:
        seen: set[tuple[str, str, date]] = set()
        for e in self.prices:
            key = (e.provider, e.model, e.effective_from)
            if key in seen:
                raise ValueError(f"重复的单价条目：{e.provider} / {e.model} / {e.effective_from}")
            seen.add(key)
        return self


class PriceTable:
    def __init__(self, entries: Iterable[PriceEntry] = ()) -> None:
        grouped: dict[tuple[str, str], list[PriceEntry]] = defaultdict(list)
        for e in entries:
            grouped[(e.provider, e.model)].append(e)
        # 每个 厂商+型号 按生效日期从新到旧排列
        self._entries = {
            k: sorted(v, key=lambda e: e.effective_from, reverse=True) for k, v in grouped.items()
        }

    @classmethod
    def load(cls, path: Path) -> PriceTable:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            parsed = PricingFile.model_validate(raw or {})
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise PricingError(f"单价表 {path} 无效：{exc}") from exc
        return cls(parsed.prices)

    def unit_price(
        self, provider: str, model: str, unit: UsageUnit, at: datetime
    ) -> UnitPrice | None:
        """at 时刻已生效的单价；缺价格时返回 None。"""
        day = at.astimezone(BEIJING_TZ).date()
        for entry in self._entries.get((provider, model), ()):
            if entry.effective_from <= day:
                price = entry.units.get(unit)
                return price if price is not None and price.price_cny is not None else None
        return None

    def cost(
        self, provider: str, model: str, unit: UsageUnit, quantity: Decimal, at: datetime
    ) -> Decimal | None:
        """估算人民币成本（保留 8 位小数）；缺价格时返回 None 并告警。"""
        price = self.unit_price(provider, model, unit, at)
        if price is None or price.price_cny is None:
            _report_missing(provider, model, unit)
            return None
        return (price.price_cny * quantity / price.per).quantize(COST_QUANTUM, ROUND_HALF_UP)


def _report_missing(provider: str, model: str, unit: UsageUnit) -> None:
    _missing_total.labels(provider=provider, model=model, unit=unit.value).inc()
    key = (provider, model, unit.value)
    if key not in _warned:
        _warned.add(key)
        log.warning("pricing_missing", provider=provider, model=model, unit=unit.value)


@lru_cache(maxsize=1)
def get_price_table() -> PriceTable:
    """进程内只加载一次；路径来自 PRICING_FILE。"""
    return PriceTable.load(get_settings().cost.pricing_file)
