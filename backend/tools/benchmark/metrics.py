"""实测用的纯计算：识别错误率、术语命中率、分位数、均值与标准差、Markdown 表格。"""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# 汉字按字计，英文单词、数字按词计
_TOKEN = re.compile(r"[㐀-䶿一-鿿豈-﫿]|[a-z0-9]+(?:['’][a-z]+)?")


def tokens(text: str) -> list[str]:
    """归一化后切分：全角转半角、转小写、去掉标点；汉字一个字一个单位，英文和数字一个词一个单位。"""
    normalized = unicodedata.normalize("NFKC", text).lower()
    return _TOKEN.findall(normalized)


def edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    """替换、删除、插入的最少次数（Levenshtein）。"""
    previous = list(range(len(hypothesis) + 1))
    for i, ref in enumerate(reference, start=1):
        current = [i] + [0] * len(hypothesis)
        for j, hyp in enumerate(hypothesis, start=1):
            current[j] = min(
                previous[j] + 1,  # 删除
                current[j - 1] + 1,  # 插入
                previous[j - 1] + (ref != hyp),  # 替换
            )
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class ErrorCount:
    errors: int
    reference_tokens: int

    @property
    def rate(self) -> float:
        return self.errors / self.reference_tokens if self.reference_tokens else 0.0


def error_count(reference: str, hypothesis: str) -> ErrorCount:
    """中英混合错误率的分子分母：中文按字、英文按词。"""
    ref = tokens(reference)
    return ErrorCount(edit_distance(ref, tokens(hypothesis)), len(ref))


def total_error_rate(counts: Iterable[ErrorCount]) -> float:
    """整体错误率 = 错误总数 / 参考单位总数（不是各条错误率的平均）。"""
    items = list(counts)
    reference = sum(c.reference_tokens for c in items)
    return sum(c.errors for c in items) / reference if reference else 0.0


def _compact(text: str) -> str:
    return "".join(tokens(text))


def term_hits(reference: str, hypothesis: str, terms: Iterable[str]) -> tuple[int, int]:
    """术语命中：参考文本里出现的每个术语，识别结果里也出现算命中。返回（命中次数, 出现次数）。

    比较时忽略大小写、空格和标点，所以 “Fast API” 与 “FastAPI” 视为相同。
    """
    ref, hyp = _compact(reference), _compact(hypothesis)
    hits = total = 0
    for term in terms:
        key = _compact(term)
        if not key:
            continue
        expected = ref.count(key)
        total += expected
        hits += min(expected, hyp.count(key))
    return hits, total


def percentile(values: Sequence[float], p: float) -> float:
    """线性插值分位数（p 取 0–100），与 numpy 的默认算法一致。"""
    if not values:
        return math.nan
    ordered = sorted(values)
    position = (len(ordered) - 1) * p / 100
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def mean_stdev(values: Sequence[float]) -> tuple[float, float]:
    """均值与样本标准差；少于 2 个值时标准差为 0。"""
    if not values:
        return math.nan, math.nan
    return statistics.fmean(values), statistics.stdev(values) if len(values) > 1 else 0.0


def table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    """渲染 Markdown 表格。"""
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def ms(value: float) -> str:
    return "-" if math.isnan(value) else f"{value:.0f} ms"


def pct(value: float) -> str:
    return "-" if math.isnan(value) else f"{value * 100:.1f}%"
