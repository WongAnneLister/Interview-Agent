"""候选人一句话里不用大模型也能确定的意图。

请面试官再说一遍：这种请求不该交给实时评估去猜，评估失败或超时时会按“换下一题”降级，
候选人说“请再说一遍”反而跳到了下一题（PR-29 语音实测，C33）。这里用规则识别，命中就直接重复。

只认短句：长回答里出现“重复”“repeat”多半是在讲技术内容（如幂等、重复下单），不能误判。
PR-30 的“我想一下 / 我说完了”也属于这一类，届时把关键词挪进配置文件。
"""

from __future__ import annotations

import re

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
