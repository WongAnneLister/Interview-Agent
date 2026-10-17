"""识别实测：字错率（中文按字、英文按词）、术语命中率、最终结果耗时，对比“无热词 / 有热词”。

数据目录里每条录音是 xxx.wav（16 kHz 单声道 16 bit）+ xxx.txt（参考文本）；术语表每行一个词，
可写成“词<TAB>权重”。有术语表时临时创建热词表，跑完删除（每个账号最多 10 个表）。
录音按实时节奏发送（每 100 ms 一包），最终结果耗时 = 最后一包音频发出到收到 task-finished。

单独运行：PYTHONPATH=tools uv run python -m benchmark.asr --data <目录> --terms <术语表>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from create_hotwords import read_words

from benchmark.common import DATA_DIR, provider_settings, read_pcm16_mono_16k, region
from benchmark.metrics import (
    ErrorCount,
    error_count,
    ms,
    pct,
    percentile,
    table,
    term_hits,
    total_error_rate,
)
from interview.providers.bailian_hotwords import BailianHotwords, Hotword
from interview.providers.base import STTProvider
from interview.providers.registry import default_factories

CHUNK_MS = 100
CHUNK_BYTES = 16000 * 2 * CHUNK_MS // 1000


@dataclass(frozen=True)
class Sample:
    name: str
    pcm: bytes
    reference: str

    @property
    def seconds(self) -> float:
        return len(self.pcm) / 32000


@dataclass(frozen=True)
class Run:
    sample: str
    hypothesis: str
    errors: ErrorCount
    hits: int
    terms: int
    final_latency_ms: float
    first_latency_ms: float | None


@dataclass(frozen=True)
class AsrResult:
    model: str
    url: str
    samples: int
    audio_seconds: float
    term_count: int
    plain: list[Run]
    hot: list[Run]  # 没有术语表时为空


def load_samples(directory: Path) -> list[Sample]:
    samples: list[Sample] = []
    for wav in sorted(directory.glob("*.wav")):
        reference = wav.with_suffix(".txt")
        if not reference.exists():
            raise ValueError(f"{wav.name} 缺少参考文本 {reference.name}")
        text = reference.read_text(encoding="utf-8").strip()
        samples.append(Sample(wav.stem, read_pcm16_mono_16k(wav), text))
    if not samples:
        raise ValueError(f"{directory} 里没有 .wav 录音（见 README 的“准备实测数据”）")
    return samples


async def recognize(
    asr: STTProvider, sample: Sample, terms: Sequence[str], vocabulary_id: str | None
) -> Run:
    ended_at = 0.0

    async def realtime() -> AsyncIterator[bytes]:
        nonlocal ended_at
        for start in range(0, len(sample.pcm), CHUNK_BYTES):
            await asyncio.sleep(CHUNK_MS / 1000)  # 等这一段“说完”再发，与实时采集一致
            yield sample.pcm[start : start + CHUNK_BYTES]
        ended_at = time.perf_counter()

    finals: list[str] = []
    async with asr.stream(realtime(), hotword_list_id=vocabulary_id) as stream:
        async for event in stream:
            if event.kind == "final":
                finals.append(event.text)
    finished_at = time.perf_counter()
    hypothesis = "".join(finals)
    hits, total = term_hits(sample.reference, hypothesis, terms)
    return Run(
        sample=sample.name,
        hypothesis=hypothesis,
        errors=error_count(sample.reference, hypothesis),
        hits=hits,
        terms=total,
        final_latency_ms=(finished_at - ended_at) * 1000,
        first_latency_ms=stream.first_latency_ms,
    )


def load_terms(terms_file: Path | None) -> list[Hotword]:
    return read_words(terms_file, 4) if terms_file and terms_file.exists() else []


async def run(samples: list[Sample], words: list[Hotword]) -> AsrResult:
    """samples / words 由调用方事先读好（load_samples、load_terms），这里只做网络调用。"""
    settings = provider_settings()
    role, vendor = settings.stt, settings.bailian
    terms = [w.text for w in words]
    asr = default_factories(settings).stt[role.provider](role)
    plain: list[Run] = []
    hot: list[Run] = []
    try:
        await asr.warmup()
        for sample in samples:
            plain.append(await recognize(asr, sample, terms, None))
            print(f"  [asr] 无热词 {sample.name} 完成", file=sys.stderr)
        if words:
            hotwords = BailianHotwords(
                api_key=vendor.api_key.get_secret_value(),
                base_url=vendor.api_base_url,
                timeout_s=role.timeout_s,
            )
            vocabulary_id = await hotwords.create(
                target_model=role.model, prefix="bench", words=words
            )
            try:
                for sample in samples:
                    hot.append(await recognize(asr, sample, terms, vocabulary_id))
                    print(f"  [asr] 有热词 {sample.name} 完成", file=sys.stderr)
            finally:
                await hotwords.delete(vocabulary_id)
                await hotwords.aclose()
    finally:
        await asr.aclose()
    return AsrResult(
        model=role.model,
        url=role.base_url,
        samples=len(samples),
        audio_seconds=sum(s.seconds for s in samples),
        term_count=len(terms),
        plain=plain,
        hot=hot,
    )


def _summary(label: str, runs: list[Run]) -> list[object]:
    hits = sum(r.hits for r in runs)
    total = sum(r.terms for r in runs)
    latencies = [r.final_latency_ms for r in runs]
    firsts = [r.first_latency_ms for r in runs if r.first_latency_ms is not None]
    return [
        label,
        pct(total_error_rate(r.errors for r in runs)),
        f"{hits}/{total}（{pct(hits / total) if total else '-'}）",
        ms(percentile(latencies, 50)),
        ms(percentile(latencies, 95)),
        ms(percentile(firsts, 50)),
    ]


def render(result: AsrResult) -> str:
    headers = ["条件", "字错率", "术语命中", "最终结果 P50", "最终结果 P95", "首个结果 P50"]
    rows = [_summary("无热词", result.plain)]
    if result.hot:
        rows.append(_summary(f"有热词（{result.term_count} 个）", result.hot))
    detail_headers = ["录音", "参考单位数", "无热词错误", "有热词错误", "最终结果耗时"]
    hot_by_name = {r.sample: r for r in result.hot}
    detail = [
        [
            r.sample,
            r.errors.reference_tokens,
            f"{r.errors.errors}（{pct(r.errors.rate)}）",
            (
                f"{hot.errors.errors}（{pct(hot.errors.rate)}）"
                if (hot := hot_by_name.get(r.sample))
                else "-"
            ),
            ms(r.final_latency_ms),
        ]
        for r in result.plain
    ]
    return "\n\n".join(
        [
            "## 语音识别",
            f"型号 `{result.model}`，地域 {region(result.url)}；{result.samples} 条录音，"
            f"共 {result.audio_seconds:.0f} 秒，按实时节奏发送。字错率中文按字、英文按词，"
            "忽略大小写和标点；最终结果耗时 = 最后一包音频发出到任务结束。",
            table(headers, rows),
            table(detail_headers, detail),
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA_DIR / "asr", help="录音与参考文本目录")
    parser.add_argument("--terms", type=Path, default=DATA_DIR / "terms.txt", help="术语表")
    args = parser.parse_args()
    result = asyncio.run(run(load_samples(args.data), load_terms(args.terms)))
    print(render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
