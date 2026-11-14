"""候选人一句话里不用大模型也能确定的意图。

请面试官再说一遍：这种请求不该交给实时评估去猜，评估失败或超时时会按“换下一题”降级，
候选人说“请再说一遍”反而跳到了下一题（PR-29 语音实测，C33）。这里用规则识别，命中就直接重复。

只认短句：长回答里出现“重复”“repeat”多半是在讲技术内容（如幂等、重复下单），不能误判。

PR-30：“我想一下”（要思考时间）和“我说完了”（立即结束本轮）的词表放在
backend/config/turn_keywords.yaml（TURN_KEYWORDS_FILE），中英各一组；“请重复”需要模式匹配，留在代码里。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import yaml

from interview.config import DEFAULT_TURN_KEYWORDS_FILE

_CJK = re.compile(r"[一-鿿]")
MAX_CJK_CHARS = 30  # 含汉字时最多这么多字
MAX_WORDS = 12  # 英文最多这么多词

_REPEAT_ZH = re.compile(
    r"(再|重新)(说|讲|问|念|读)(一)?(遍|次|下)"
    r"|请重复|重复(一下|一遍|一次)"  # 不认单独的“重复下”：会误中“重复下单”
    r"|没(有)?听(清|清楚|懂|到|明白)"
    r"|(题目|问题)是(什么|啥)"
    r"|刚才(问|说)的是(什么|啥)"
)
_REPEAT_EN = re.compile(
    r"\b(say|ask|read) (it|that|the question) again\b"
    r"|\bsay again\b"
    r"|\b(can|could|would) you (please )?repeat\b"
    r"|\bplease repeat\b|\brepeat (that|it|the question|please)\b"
    r"|\bcome again\b|\bpardon\b|\bone more time\b"
    r"|\bdid ?n[o']?t (hear|catch|get) (it|that|the question|you)\b"
    r"|\bwhat('?s| is| was) the question\b"
    r"|\bwhat('?s| is| was) your question\b",
    re.IGNORECASE,
)


def _short(text: str) -> bool:
    if _CJK.search(text):
        return len(text) <= MAX_CJK_CHARS
    return len(text.split()) <= MAX_WORDS


def is_repeat_request(text: str) -> bool:
    """候选人这一轮是不是在请面试官再说一遍（中英文都认，候选人常常混说）。"""
    stripped = text.strip()
    if not stripped or not _short(stripped):
        return False
    return bool(_REPEAT_ZH.search(stripped) or _REPEAT_EN.search(stripped))


_NOISE = re.compile(r"[\W_]+", re.UNICODE)  # 标点、空白：匹配时都忽略


def _squash(text: str) -> str:
    return _NOISE.sub("", unicodedata.normalize("NFKC", text).casefold())


@dataclass(frozen=True)
class TurnKeywords:
    think: tuple[str, ...]
    done: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> TurnKeywords:
        raw = cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")) or {})

        def phrases(group: str) -> tuple[str, ...]:
            by_language = cast(dict[str, list[str]], raw.get(group) or {})
            items = [_squash(p) for words in by_language.values() for p in words or []]
            if not items:
                raise ValueError(f"{path}：{group} 词表为空")
            return tuple(sorted(set(items), key=len, reverse=True))

        return cls(think=phrases("think"), done=phrases("done"))


@lru_cache(maxsize=1)
def default_keywords() -> TurnKeywords:
    return TurnKeywords.load(DEFAULT_TURN_KEYWORDS_FILE)


def is_think_request(text: str, keywords: TurnKeywords | None = None) -> bool:
    """候选人这一轮是不是在要思考时间（短句里含词表里的说法）。"""
    stripped = text.strip()
    if not stripped or not _short(stripped):
        return False
    squashed = _squash(stripped)
    return any(phrase in squashed for phrase in (keywords or default_keywords()).think)


def ends_with_done(text: str, keywords: TurnKeywords | None = None) -> bool:
    """这段转写是不是以“我说完了 / that's all”这类话收尾（忽略句尾标点）。"""
    squashed = _squash(text)
    return bool(squashed) and any(
        squashed.endswith(phrase) for phrase in (keywords or default_keywords()).done
    )
