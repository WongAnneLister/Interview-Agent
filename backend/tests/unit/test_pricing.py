"""单价表：加载校验、按单位计价（含缓存 token）、生效日期选择、缺价格时告警不中断。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from prometheus_client import REGISTRY
from structlog.testing import capture_logs

from interview.config import DEFAULT_PRICING_FILE
from interview.db.models import UsageUnit
from interview.observability.pricing import PriceTable, PricingError

PRICING_YAML = """
prices:
  - provider: bailian
    model: qwen-test
    effective_from: 2026-01-01
    source: https://example.com/pricing
    units:
      input_tokens: {price_cny: 0.8, per: 1000000}
      cached_tokens: {price_cny: 0.32, per: 1000000}
      output_tokens: {price_cny: 2, per: 1000000}
      reasoning_tokens: {price_cny: null, per: 1000000}
  - provider: bailian
    model: qwen-test
    effective_from: 2026-10-15
    units:
      input_tokens: {price_cny: 0.4, per: 1000000}
  - provider: bailian
    model: tts-test
    effective_from: 2026-01-01
    units:
      characters: {price_cny: 2, per: 10000}
      audio_seconds: {price_cny: 0.001}
"""

M = Decimal(1_000_000)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


@pytest.fixture
def table(tmp_path: Path) -> PriceTable:
    path = tmp_path / "pricing.yaml"
    path.write_text(PRICING_YAML, encoding="utf-8")
    return PriceTable.load(path)


def test_cost_per_unit_including_cached_tokens(table: PriceTable) -> None:
    t = at("2026-06-01T12:00:00+08:00")
    assert table.cost("bailian", "qwen-test", UsageUnit.INPUT_TOKENS, M, t) == Decimal("0.8")
    # 缓存命中的输入按缓存单价计，不按普通输入价
    assert table.cost("bailian", "qwen-test", UsageUnit.CACHED_TOKENS, M, t) == Decimal("0.32")
    assert table.cost("bailian", "qwen-test", UsageUnit.OUTPUT_TOKENS, Decimal(1500), t) == (
        Decimal("0.003")
    )
    assert table.cost("bailian", "tts-test", UsageUnit.CHARACTERS, Decimal(500), t) == Decimal(
        "0.1"
    )
    # per 默认为 1；小数秒
    assert table.cost("bailian", "tts-test", UsageUnit.AUDIO_SECONDS, Decimal("12.5"), t) == (
        Decimal("0.0125")
    )


def test_yaml_floats_are_exact_decimals(table: PriceTable) -> None:
    price = table.unit_price(
        "bailian", "qwen-test", UsageUnit.INPUT_TOKENS, at("2026-06-01T00:00:00+08:00")
    )
    assert price is not None
    assert price.price_cny == Decimal("0.8")


def test_cost_is_rounded_to_eight_places(table: PriceTable) -> None:
    cost = table.cost(
        "bailian", "qwen-test", UsageUnit.INPUT_TOKENS, Decimal(1), at("2026-06-01T00:00+08:00")
    )
    assert cost == Decimal("0.00000080")


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        ("2026-10-14T23:59:59+08:00", Decimal("0.8")),  # 新价生效前一天
        ("2026-10-15T00:00:00+08:00", Decimal("0.4")),  # 生效当天（北京时间）
        ("2026-10-14T16:00:00+00:00", Decimal("0.4")),  # UTC 前一天 16 点 = 北京时间当天 0 点
        ("2027-03-01T00:00:00+08:00", Decimal("0.4")),  # 之后取最新一条
    ],
)
def test_effective_date_selection(table: PriceTable, when: str, expected: Decimal) -> None:
    assert table.cost("bailian", "qwen-test", UsageUnit.INPUT_TOKENS, M, at(when)) == expected


def test_newer_entry_replaces_whole_model(table: PriceTable) -> None:
    # 新条目只列了 input_tokens，output_tokens 不回退到旧价格
    t = at("2026-11-01T00:00:00+08:00")
    assert table.cost("bailian", "qwen-test", UsageUnit.OUTPUT_TOKENS, M, t) is None


@pytest.mark.parametrize(
    ("provider", "model", "unit", "when"),
    [
        ("bailian", "unknown-model", UsageUnit.INPUT_TOKENS, "2026-06-01T00:00:00+08:00"),
        ("other", "qwen-test", UsageUnit.INPUT_TOKENS, "2026-06-01T00:00:00+08:00"),
        ("bailian", "qwen-test", UsageUnit.AUDIO_SECONDS, "2026-06-01T00:00:00+08:00"),
        ("bailian", "qwen-test", UsageUnit.REASONING_TOKENS, "2026-06-01T00:00:00+08:00"),
        ("bailian", "qwen-test", UsageUnit.INPUT_TOKENS, "2025-12-31T23:00:00+08:00"),
    ],
)
def test_missing_price_returns_none(
    table: PriceTable, provider: str, model: str, unit: UsageUnit, when: str
) -> None:
    assert table.cost(provider, model, unit, M, at(when)) is None


def test_missing_price_warns_once_and_counts_every_time(table: PriceTable) -> None:
    labels = {"provider": "bailian", "model": "warn-once", "unit": "input_tokens"}
    before = REGISTRY.get_sample_value("interview_pricing_missing_total", labels) or 0
    t = datetime.now(UTC)
    with capture_logs() as logs:
        for _ in range(3):
            assert table.cost("bailian", "warn-once", UsageUnit.INPUT_TOKENS, M, t) is None
    warnings = [e for e in logs if e["event"] == "pricing_missing"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert REGISTRY.get_sample_value("interview_pricing_missing_total", labels) == before + 3


@pytest.mark.parametrize(
    "body",
    [
        "prices: [{provider: p, model: m, effective_from: 2026-01-01, "
        "units: {gpu_hours: {price_cny: 1}}}]",
        "prices: [{provider: p, model: m, effective_from: 2026-01-01, "
        "units: {input_tokens: {price_cny: -1}}}]",
        "prices: [{provider: p, model: m, effective_from: 2026-01-01, "
        "units: {input_tokens: {price_cny: 1, per: 0}}}]",
        "prices: [{provider: p, model: m, effective_from: 2026-01-01, units: {}},"
        " {provider: p, model: m, effective_from: 2026-01-01, units: {}}]",
        "prices: [{provider: p, model: m, units: {}}]",
        "price: []",
        "prices: [",
    ],
)
def test_invalid_file_raises(tmp_path: Path, body: str) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(PricingError, match=r"pricing\.yaml"):
        PriceTable.load(path)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(PricingError):
        PriceTable.load(tmp_path / "nope.yaml")


def test_repository_pricing_file_is_valid() -> None:
    PriceTable.load(DEFAULT_PRICING_FILE)
