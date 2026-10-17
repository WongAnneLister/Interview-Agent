"""运行全部实测并汇总成 docs/benchmarks/<YYYY-MM-DD>.md（同一天多次运行时加上时间）。

用法：make benchmark（见 README 的“厂商实测”）。--skip-asr / --skip-tts / --skip-llm 跳过某一项；
没有录音数据时自动跳过识别。实测直接调用适配器，不写用量表。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from benchmark import asr, llm, tts
from benchmark.common import (
    DATA_DIR,
    REPORT_DIR,
    git_commit,
    machine,
    now,
    provider_settings,
    region,
)
from benchmark.metrics import table


def environment() -> str:
    settings = provider_settings()
    rows: list[list[object]] = []
    for label, role in (
        ("前台大模型", settings.llm_front),
        ("评估大模型", settings.llm_eval),
        ("备用大模型", settings.llm_fallback),
        ("语音识别", settings.stt),
        ("语音合成", settings.tts),
    ):
        vendor = settings.deepseek if role.provider == "deepseek" else settings.bailian
        url = role.base_url or vendor.base_url
        rows.append([label, role.provider or "-", role.model or "-", region(url)])
    started = now()
    return "\n\n".join(
        [
            f"# 厂商实测 {started:%Y-%m-%d}",
            f"运行时间 {started:%Y-%m-%d %H:%M}（北京时间），代码版本 `{git_commit()}`，"
            f"机器 {machine()}。单次结果受网络与本机负载影响，"
            "对比时以同一天、同一网络下的结果为准。",
            table(["角色", "厂商", "型号", "地域"], rows),
        ]
    )


def report_path() -> Path:
    path = REPORT_DIR / f"{now():%Y-%m-%d}.md"
    return path if not path.exists() else REPORT_DIR / f"{now():%Y-%m-%d_%H%M}.md"


async def collect(args: argparse.Namespace) -> list[str]:
    sections = [environment()]
    if args.skip_asr:
        pass
    elif not args.asr_data.is_dir() or not any(args.asr_data.glob("*.wav")):
        sections.append(f"## 语音识别\n\n跳过：`{args.asr_data}` 下没有录音。")
    else:
        samples = asr.load_samples(args.asr_data)
        result = await asr.run(samples, asr.load_terms(args.terms))
        sections.append(asr.render(result))
    if not args.skip_tts:
        sections.append(tts.render(await tts.run()))
    if not args.skip_llm:
        sections.append(llm.render(await llm.run(args.model)))
    return sections


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asr-data", type=Path, default=DATA_DIR / "asr")
    parser.add_argument("--terms", type=Path, default=DATA_DIR / "terms.txt")
    parser.add_argument("--model", action="append", default=[], help="追加大模型候选")
    parser.add_argument("--skip-asr", action="store_true")
    parser.add_argument("--skip-tts", action="store_true")
    parser.add_argument("--skip-llm", action="store_true")
    args = parser.parse_args()
    path = report_path()
    sections = asyncio.run(collect(args))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    print(f"报告已写入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
