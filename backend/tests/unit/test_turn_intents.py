"""请面试官再说一遍的识别：短句里的明确请求才算，长回答里的技术词不算。"""

from __future__ import annotations

import pytest

from interview.domain.turn_intents import is_repeat_request


@pytest.mark.parametrize(
    "text",
    [
        "Uh, what's your question? Please say it again.",  # PR-29 实测原话
        "Sorry, could you repeat the question?",
        "Can you say that again?",
        "I didn't catch that.",
        "Pardon?",
        "Come again?",
        "What was the question?",
        "我没听清，能再说一遍吗？",
        "请重复一下题目",
        "刚才问的是什么？",
        "题目是什么",
        "嗯，再问一次",
        "不好意思没听懂",
    ],
)
def test_repeat_requests(text: str) -> None:
    assert is_repeat_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Hello, can you hear me?",
        "I built an idempotent API so repeat orders are not charged twice.",
        "We had to repeat the deployment three times before it stabilized.",
        "我们做了幂等，避免重复下单。",
        "我重新设计了一遍缓存结构。",
        # 长回答里顺带一句也不算：已经是在答题了
        "I didn't catch that bug at first because the logs were sampled, "
        "so we added tracing and found the race between the two writers.",
        "我一开始没听清需求，后来和产品一起把订单状态机重新梳理了一遍，"
        "补上了超时取消和重复回调的处理。",
    ],
)
def test_not_repeat_requests(text: str) -> None:
    assert not is_repeat_request(text)
