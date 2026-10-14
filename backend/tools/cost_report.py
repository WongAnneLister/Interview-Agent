"""一场面试的成本拆解：按角色、按厂商、按 角色 + 厂商 + 型号 的明细，以及总计和每分钟成本。

用法：make cost id=<面试ID>（需要隧道；按 .env 的 DEFAULT_USER_ID 查询）。
加 --json 输出机器可读格式。
成本是按 config/pricing.yaml 估算的人民币金额；缺价格的用量不计入，会单独提示条数。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from interview.config import DEFAULT_ENV_FILE, AppSettings, DatabaseSettings
from interview.db.session import create_engine, make_sessionmaker
from interview.observability.usage import CostBreakdown, cost_breakdown


def _money(value: Decimal) -> str:
    return f"¥{value:.4f}"


def render(interview_id: uuid.UUID, b: CostBreakdown) -> str:
    out = [f"面试 {interview_id}", ""]
    if not b.lines:
        return "\n".join([*out, "没有用量记录。"])
    for title, groups in (("按角色", b.by_role), ("按厂商", b.by_provider)):
        out.append(title)
        out += [f"  {name:<16}{_money(cost):>14}" for name, cost in groups.items()]
        out.append("")
    out.append("明细（角色 / 厂商 / 型号）")
    for line in b.lines:
        name = f"{line.role} / {line.provider} / {line.model}"
        note = f"  （{line.unpriced_events} 条缺价格）" if line.unpriced_events else ""
        out.append(f"  {name:<48}{_money(line.cost_cny):>14}  {line.events} 条{note}")
    out += ["", f"总计      {_money(b.total_cny)}"]
    duration = f"{b.duration_min} 分钟" if b.duration_min else "未知（面试未开始或未结束）"
    out.append(f"时长      {duration}")
    if b.cost_per_min_cny is not None:
        out.append(f"每分钟    {_money(b.cost_per_min_cny)}")
    if b.unpriced_events:
        out.append(
            f"注意：{b.unpriced_events} 条用量缺价格，成本被低估（补充 config/pricing.yaml）"
        )
    return "\n".join(out)


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


async def run(interview_id: uuid.UUID, as_json: bool) -> int:
    user_id = AppSettings(_env_file=DEFAULT_ENV_FILE).default_user_id  # pyright: ignore[reportCallIssue]
    if user_id is None:
        print("DEFAULT_USER_ID 未配置：先运行 make seed", file=sys.stderr)
        return 1
    url = DatabaseSettings(_env_file=DEFAULT_ENV_FILE).url.get_secret_value()  # pyright: ignore[reportCallIssue]
    engine = create_engine(url)
    try:
        b = await cost_breakdown(
            interview_id, user_id=user_id, sessionmaker=make_sessionmaker(engine)
        )
    finally:
        await engine.dispose()
    if as_json:
        print(json.dumps(asdict(b), ensure_ascii=False, indent=2, default=_json_default))
    else:
        print(render(interview_id, b))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("interview_id", type=uuid.UUID, help="面试 ID")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    return asyncio.run(run(args.interview_id, args.json))


if __name__ == "__main__":
    sys.exit(main())
