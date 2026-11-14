"""候选人一句话里的明确意图：请重复（正则）、我想一下 / 我说完了（词表）；短句才算。"""

from __future__ import annotations

from pathlib import Path

import pytest

from interview.domain.turn_intents import (
    TurnKeywords,
    ends_with_done,
    is_repeat_request,
    is_think_request,
)


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


# ---- 我想一下 / 我说完了（PR-30，词表在 config/turn_keywords.yaml）----


@pytest.mark.parametrize(
    "text",
    [
        "我想一下。",
        "嗯，让我想想",
        "Let me think.",
        "Hmm, give me a moment.",
        "稍等一下，我考虑一下",
    ],
)
def test_think_requests(text: str) -> None:
    assert is_think_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "我想一下当时的情况，我们的订单量在大促时是平时的十倍，所以先扩了消息队列的分区。",
        "Let me think about how we partitioned the queue: we used the order id as the key "
        "so every order stays in one partition.",
        "我做过支付系统。",
    ],
)
def test_not_think_requests(text: str) -> None:
    assert not is_think_request(text)


@pytest.mark.parametrize(
    "text",
    ["……所以吞吐提升了三倍。就这些。", "我说完了", "And that's all.", "That's it!", "I'm done"],
)
def test_done_phrases_at_the_end(text: str) -> None:
    assert ends_with_done(text)


@pytest.mark.parametrize(
    "text",
    ["", "就这些问题我们当时讨论了很久，最后选了 Kafka", "That's all I knew at first, but then"],
)
def test_done_phrase_not_at_the_end(text: str) -> None:
    assert not ends_with_done(text)


def test_keywords_file_is_configurable(tmp_path: Path) -> None:
    custom = tmp_path / "turn_keywords.yaml"
    custom.write_text("think:\n  zh: [容我想想]\ndone:\n  en: [over to you]\n", encoding="utf-8")
    keywords = TurnKeywords.load(custom)
    assert is_think_request("容我想想", keywords) and not is_think_request("我想一下", keywords)
    assert ends_with_done("Done with the design. Over to you!", keywords)


def test_empty_keyword_group_is_rejected(tmp_path: Path) -> None:
    broken = tmp_path / "turn_keywords.yaml"
    broken.write_text("think:\n  zh: []\ndone:\n  zh: [说完了]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="think"):
        TurnKeywords.load(broken)
