"""口语规范化：markdown、百分号、单位、缩写和符号的规则表。"""

from __future__ import annotations

import pytest

from interview.db.models import Language
from interview.domain.speech_text import normalize


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("**加粗**", "加粗"),
        ("__下划线__", "下划线"),
        ("`code`", "code"),
        ("# 标题", "标题"),
        ("- 列表项", "列表项"),
        ("1. 第一项", "第一项"),
        ("[链接文字](https://example.com)", "链接文字"),
        ("- **你好**，`世界`。", "你好，世界。"),
    ],
)
def test_markdown_is_removed(text: str, expected: str) -> None:
    assert normalize(text, Language.ZH) == expected


@pytest.mark.parametrize(
    ("text", "language", "expected"),
    [
        ("95%", Language.ZH, "百分之九十五"),
        ("95 %", Language.ZH, "百分之九十五"),
        ("100%", Language.ZH, "百分之一百"),
        ("99.5%", Language.ZH, "百分之九十九点五"),
        ("95%", Language.EN, "95 percent"),
    ],
)
def test_percent(text: str, language: Language, expected: str) -> None:
    assert normalize(text, language) == expected


@pytest.mark.parametrize(
    ("text", "language", "expected"),
    [
        ("3ms", Language.ZH, "3毫秒"),
        ("3 ms", Language.ZH, "3毫秒"),
        ("2s", Language.ZH, "2秒"),
        ("5min", Language.ZH, "5分钟"),
        ("10GB", Language.ZH, "10 GB"),
        ("QPS 1w", Language.ZH, "QPS 1万"),
        ("3ms", Language.EN, "3 milliseconds"),
        ("2s", Language.EN, "2 seconds"),
        # 单位紧跟汉字是中文里最常见的写法，\b 在汉字前不成立，曾经整条不生效
        ("延迟3ms左右", Language.ZH, "延迟3毫秒左右"),
        ("重试5s后", Language.ZH, "重试5秒后"),
        ("内存10GB的机器", Language.ZH, "内存10 GB的机器"),
        ("took 2.5s", Language.EN, "took 2.5 seconds"),
        ("in the 1990s", Language.EN, "in the 1990s"),
        ("the 2010s", Language.EN, "the 2010s"),
    ],
)
def test_units(text: str, language: Language, expected: str) -> None:
    assert normalize(text, language) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("P95", "P九十五"),
        ("P99", "P九十九"),
        ("P50 和 P99", "P五十 和 P九十九"),
        ("P95延迟是多少", "P九十五延迟是多少"),
        ("看一下P99", "看一下P九十九"),
        ("GP100 显卡", "GP100 显卡"),
        ("QPS、TPS、API、SQL", "QPS、TPS、API、SQL"),
        ("OpenAPI 的 API", "OpenAPI 的 API"),
    ],
)
def test_abbreviations_are_whole_word(text: str, expected: str) -> None:
    assert normalize(text, Language.ZH) == expected


@pytest.mark.parametrize(
    ("text", "language", "expected"),
    [
        ("A & B", Language.ZH, "A 和 B"),
        ("前端/后端", Language.ZH, "前端或后端"),
        ("3~5", Language.ZH, "3到5"),
        ("A->B", Language.ZH, "A到B"),
        ("A=>B", Language.ZH, "A到B"),
        ("A & B", Language.EN, "A and B"),
        ("3~5", Language.EN, "3 to 5"),
        ("A->B", Language.EN, "A to B"),
    ],
)
def test_symbols(text: str, language: Language, expected: str) -> None:
    assert normalize(text, language) == expected


def test_english_terms_and_already_spoken_text_are_unchanged() -> None:
    terms = "Kubernetes、PostgreSQL、Redis、FastAPI"
    assert normalize(terms, Language.ZH) == terms
    spoken = "这已经是一句口语化的回答，不需要再改。"
    assert normalize(spoken, Language.ZH) == spoken


def test_whitespace_is_merged() -> None:
    assert normalize("  多个   空白\n和\t制表符  ", Language.ZH) == "多个 空白 和 制表符"


def test_rules_run_in_order() -> None:
    assert normalize("**P95** 的 3ms 和 95%", Language.ZH) == "P九十五 的 3毫秒 和 百分之九十五"
