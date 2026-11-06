"""评测结果：写 evals/results/<run_id>.json 和 .md，并与上一次结果对比。"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from evals.data import EVALS_DIR
from evals.metrics import Metric, SessionMetrics
from interview.observability.usage import CostBreakdown

RESULTS_DIR = EVALS_DIR / "results"
TOLERANCE = 0.05  # 小于这个变化不算退化（裁判和模型本身有波动）


class RunResult(BaseModel):
    run_id: str
    mode: str
    personas: list[str]
    fixtures: list[str]
    prompt_versions: dict[str, int]
    models: dict[str, str]
    metrics: list[Metric]
    sessions: list[SessionMetrics]
    cost_cny: str
    cost_by_role: dict[str, str]
    unpriced_events: int
    wall_s: float
    langfuse: str = ""


UNKNOWN_COST = "未知"


def money(cost_cny: str) -> str:
    return cost_cny if cost_cny == UNKNOWN_COST else f"¥{cost_cny}"


def cost_fields(cost: CostBreakdown | None) -> dict[str, Any]:
    """cost 为空表示汇总时连不上数据库。"""
    if cost is None:
        return {"cost_cny": UNKNOWN_COST, "cost_by_role": {}, "unpriced_events": 0}
    return {
        "cost_cny": f"{cost.total_cny:.4f}",
        "cost_by_role": {k: f"{v:.4f}" for k, v in cost.by_role.items()},
        "unpriced_events": cost.unpriced_events,
    }


def previous(run_id: str, directory: Path = RESULTS_DIR) -> RunResult | None:
    """上一次（文件名排序在本次之前的最近一份）结果。"""
    earlier = sorted(p for p in directory.glob("*.json") if p.stem < run_id)
    if not earlier:
        return None
    return RunResult.model_validate_json(earlier[-1].read_text(encoding="utf-8"))


def compare(current: list[Metric], before: RunResult | None) -> dict[str, str]:
    """每个指标与上次的变化；退化的标 ⚠。"""
    if before is None:
        return {}
    old = {m.key: m.value for m in before.metrics}
    notes: dict[str, str] = {}
    for m in current:
        prev = old.get(m.key)
        if m.value is None or prev is None:
            continue
        delta = m.value - prev
        worse = -delta if m.higher_is_better else delta
        mark = " ⚠ 退化" if worse > TOLERANCE else ""
        notes[m.key] = f"{prev:g} → {m.value:g}（{delta:+.3g}）{mark}"
    return notes


def _cell(text: str, limit: int = 120) -> str:
    """表格单元格：压成一行、转义竖线并截断（完整错误在 .json 里）。"""
    flat = " ".join(text.split()).replace("|", "\\|")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def render(result: RunResult, before: RunResult | None) -> str:
    changes = compare(result.metrics, before)
    status = {True: "✅", False: "❌", None: "·"}
    out = [
        f"# 文字层评测 {result.run_id}",
        "",
        f"- 模式：{result.mode}；人设：{', '.join(result.personas)}；"
        f"材料：{', '.join(result.fixtures)}",
        f"- 场次：{len(result.sessions)}；耗时 {result.wall_s:.0f} 秒；"
        f"估算费用 {money(result.cost_cny)}"
        + (
            f"（{result.unpriced_events} 条用量缺价格，费用偏低）" if result.unpriced_events else ""
        ),
        f"- 模型：{', '.join(f'{k}={v}' for k, v in result.models.items())}",
        f"- 提示词：{', '.join(f'{k}@{v}' for k, v in result.prompt_versions.items())}",
        f"- 对比上次：{before.run_id if before else '无（首次）'}；{result.langfuse}",
        "",
        "## 指标",
        "",
        "| | 指标 | 数值 | 目标 | 说明 | 与上次比 |",
        "|---|---|---|---|---|---|",
    ]
    for m in result.metrics:
        out.append(
            f"| {status[m.passed]} | {m.name} | {_fmt(m.value)} | {m.target} | {m.detail} "
            f"| {changes.get(m.key, '')} |"
        )
    out += ["", "## 费用（按角色）", ""]
    out += [f"- {role}：¥{cost}" for role, cost in result.cost_by_role.items()]
    out += [
        "",
        "## 各场",
        "",
        "| 材料 | 人设 | 总分 | 预期内 | 轮数 | 虚拟时长 | 追问合理 | 题目相关 | 证据 | 泄露 "
        "| 备注 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in result.sessions:
        remark = _cell(s.error) if s.error else ("达到轮数上限" if s.hit_turn_cap else "")
        out.append(
            f"| {s.fixture} | {s.persona} | {_fmt(s.overall_score)} | "
            f"{status[s.in_expected_range]} | {s.candidate_turns} | {s.clock_ms / 60000:.1f} 分 | "
            f"{s.follow_ups_ok}/{s.follow_ups} | {s.questions_relevant}/{s.questions} | "
            f"{s.evidence_verified}/{s.evidence_quotes} | {_fmt(s.leaks)} | {remark} |"
        )
    notes = [(s, n) for s in result.sessions for n in s.notes]
    if notes:
        out += ["", "## 裁判判为不合格（供人工抽检）", ""]
        out += [f"- {s.fixture}/{s.persona}：{n}" for s, n in notes]
    return "\n".join(out) + "\n"


def write(result: RunResult, directory: Path = RESULTS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    before = previous(result.run_id, directory)
    (directory / f"{result.run_id}.json").write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2, default=_default)
        + "\n",
        encoding="utf-8",
    )
    path = directory / f"{result.run_id}.md"
    path.write_text(render(result, before), encoding="utf-8")
    return path


def _default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)
