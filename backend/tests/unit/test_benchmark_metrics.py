"""实测脚本的纯计算：中英混合切分、错误率、术语命中、分位数、标准差、表格。"""

import math

import pytest
from benchmark.metrics import (
    ErrorCount,
    edit_distance,
    error_count,
    mean_stdev,
    percentile,
    table,
    term_hits,
    tokens,
    total_error_rate,
)


def test_tokens_split_han_by_char_and_english_by_word() -> None:
    assert tokens("我用 FastAPI，写了 3 个服务。") == [
        "我",
        "用",
        "fastapi",
        "写",
        "了",
        "3",
        "个",
        "服",
        "务",
    ]
    assert tokens("Ｐｙｔｈｏｎ don't") == ["python", "don't"]  # 全角转半角，保留缩写


def test_edit_distance() -> None:
    assert edit_distance(list("abc"), list("abc")) == 0
    assert edit_distance(list("abc"), list("axc")) == 1  # 替换
    assert edit_distance(list("abc"), list("ac")) == 1  # 删除
    assert edit_distance(list("abc"), list("abxc")) == 1  # 插入
    assert edit_distance([], list("ab")) == 2


def test_error_rate_mixes_chars_and_words() -> None:
    count = error_count("数据库用 MySQL 跟 PostgreSQL", "数据库用 My circle 跟 PostgreSQL")
    # 参考 7 个单位：数 据 库 用 mysql 跟 postgresql；mysql → my、circle：1 次替换 + 1 次插入
    assert count == ErrorCount(errors=2, reference_tokens=7)
    assert count.rate == pytest.approx(2 / 7)


def test_total_error_rate_is_weighted_by_reference_length() -> None:
    counts = [ErrorCount(1, 10), ErrorCount(3, 30)]
    assert total_error_rate(counts) == pytest.approx(4 / 40)
    assert total_error_rate([]) == 0.0


def test_term_hits_ignore_case_spaces_and_punctuation() -> None:
    reference = "用 FastAPI 和 MySQL，MySQL 做主库。"
    hypothesis = "用 Fast API 和 My circle，mysql 做主库。"
    assert term_hits(reference, hypothesis, ["FastAPI", "MySQL", "Redis"]) == (2, 3)


def test_percentile_linear_interpolation() -> None:
    values = [100.0, 200.0, 300.0, 400.0]
    assert percentile(values, 50) == pytest.approx(250)
    assert percentile(values, 95) == pytest.approx(385)
    assert percentile([42.0], 95) == 42.0
    assert math.isnan(percentile([], 50))


def test_mean_stdev() -> None:
    assert mean_stdev([7.0, 8.0, 9.0]) == (8.0, 1.0)
    assert mean_stdev([7.0]) == (7.0, 0.0)


def test_table() -> None:
    assert table(["a", "b"], [[1, "x"]]) == "| a | b |\n|---|---|\n| 1 | x |"
