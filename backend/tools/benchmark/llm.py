"""大模型实测：各候选型号跑 20 次短对话测首 token 耗时 P50 / P95；同一段回答打分 5 次看标准差。

候选型号默认取配置里的前台、评估、备用三个角色（去重）；--model 厂商:型号 可以追加，
追加的型号沿用前台角色的地址、超时与 EXTRA_BODY（厂商不同时用该厂商的默认地址）。
评分只用一个简单提示词，要求输出 1–10 的整数；解析不出整数记为失败。正式的结构化评估在 PR-11 之后。

单独运行：PYTHONPATH=tools uv run python -m benchmark.llm [--model bailian:qwen3.8-plus]
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass, field

from benchmark.common import provider_settings, region
from benchmark.metrics import mean_stdev, ms, percentile, table
from interview.config import ProviderRoleSettings
from interview.providers.base import ChatMessage, LLMProvider, ProviderError
from interview.providers.registry import default_factories

CHAT_RUNS = 20
SCORE_RUNS = 5
SCORE_TEMPERATURE = 0.3

INTERVIEWER = "你是技术面试官。根据候选人的回答，用一句话追问一个具体细节，不要寒暄。"
ANSWERS = [
    "我负责订单服务，用 Redis 做了库存预扣。",
    "我们把同步调用改成了消息队列，削峰效果明显。",
    "I led the migration from MySQL to PostgreSQL last year.",
    "我做过一个推荐系统的召回模块，离线用 Spark 训练。",
]
SCORER = (
    "你是面试评估员。按 1–10 分评价候选人回答的技术深度与清晰度。"
    "只输出一个整数，不要输出任何其他内容。"
)
SCORED_QUESTION = "线上消息丢失你们是怎么解决的？"
SCORED_ANSWER = (
    "我们用本地消息表，也就是 Outbox Pattern。业务和消息在同一个事务里写库，"
    "定时任务扫描未发送的消息再投递到 MQ。重复发送靠状态机和消费端幂等来处理。"
)


@dataclass
class ModelResult:
    label: str
    provider: str
    model: str
    url: str
    first_ms: list[float] = field(default_factory=list[float])
    total_ms: list[float] = field(default_factory=list[float])
    chat_errors: list[str] = field(default_factory=list[str])
    scores: list[int] = field(default_factory=list[int])
    score_failures: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class Candidate:
    label: str
    role: ProviderRoleSettings


def candidates(extra: list[str]) -> list[Candidate]:
    settings = provider_settings()
    found: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for label in ("llm_front", "llm_eval", "llm_fallback"):
        role: ProviderRoleSettings = getattr(settings, label)
        key = (role.provider, role.model)
        if role.configured and key not in seen:
            seen.add(key)
            found.append(Candidate(label, role))
    for spec in extra:
        provider, _, model = spec.partition(":")
        if not model or (provider, model) in seen:
            continue
        base = settings.llm_front
        same_vendor = base.provider == provider
        role = base.model_copy(
            update={
                "provider": provider,
                "model": model,
                "base_url": base.base_url if same_vendor else "",
                "extra_body": base.extra_body if same_vendor else {},
            }
        )
        seen.add((provider, model))
        found.append(Candidate(f"追加 {provider}:{model}", role))
    return found


async def _chat(llm: LLMProvider, result: ModelResult, index: int) -> None:
    loop = asyncio.get_running_loop()
    messages: list[ChatMessage] = [
        {"role": "system", "content": INTERVIEWER},
        {"role": "user", "content": ANSWERS[index % len(ANSWERS)]},
    ]
    started = loop.time()
    try:
        async with llm.stream_chat(messages, max_tokens=80, temperature=0.7) as stream:
            _ = [chunk async for chunk in stream]
    except ProviderError as exc:
        result.chat_errors.append(type(exc).__name__)
        return
    if stream.first_latency_ms is not None:
        result.first_ms.append(stream.first_latency_ms)
    result.total_ms.append((loop.time() - started) * 1000)


async def _score(llm: LLMProvider, result: ModelResult) -> None:
    messages: list[ChatMessage] = [
        {"role": "system", "content": SCORER},
        {"role": "user", "content": f"问题：{SCORED_QUESTION}\n回答：{SCORED_ANSWER}"},
    ]
    try:
        reply = await llm.complete(messages, max_tokens=400, temperature=SCORE_TEMPERATURE)
    except ProviderError as exc:
        result.score_failures.append(type(exc).__name__)
        return
    match = re.search(r"(?<!\d)(10|[1-9])(?!\d)", reply.text)  # 中文里 \b 不可靠
    if match is None:
        result.score_failures.append("无法解析")
    else:
        result.scores.append(int(match.group(1)))


async def run(extra_models: list[str] | None = None) -> list[ModelResult]:
    settings = provider_settings()
    factories = default_factories(settings).llm
    results: list[ModelResult] = []
    for candidate in candidates(extra_models or []):
        role = candidate.role
        llm = factories[role.provider](role)
        result = ModelResult(candidate.label, role.provider, role.model, role.base_url)
        try:
            for index in range(CHAT_RUNS):
                await _chat(llm, result, index)
            for _ in range(SCORE_RUNS):
                await _score(llm, result)
        finally:
            await llm.aclose()
        print(f"  [llm] {role.provider}/{role.model} 完成", file=sys.stderr)
        results.append(result)
    return results


def render(results: list[ModelResult]) -> str:
    latency = table(
        ["角色", "型号", "地域", "成功", "首 token P50", "首 token P95", "完整回答 P50"],
        [
            [
                r.label,
                f"{r.provider}/{r.model}",
                region(r.url),
                f"{len(r.first_ms)}/{CHAT_RUNS}",
                ms(percentile(r.first_ms, 50)),
                ms(percentile(r.first_ms, 95)),
                ms(percentile(r.total_ms, 50)),
            ]
            for r in results
        ],
    )
    scoring = table(
        ["型号", "5 次打分", "均值", "标准差", "失败"],
        [
            [
                f"{r.provider}/{r.model}",
                " ".join(str(s) for s in r.scores) or "-",
                f"{mean_stdev(r.scores)[0]:.1f}" if r.scores else "-",
                f"{mean_stdev(r.scores)[1]:.2f}" if r.scores else "-",
                ", ".join(r.score_failures) or "0",
            ]
            for r in results
        ],
    )
    return "\n\n".join(
        [
            "## 大模型",
            f"每个型号 {CHAT_RUNS} 次短对话（面试官追问一句，max_tokens=80，temperature=0.7，"
            "流式），首 token = 发出请求到收到第一个正文片段；思考内容不计入正文。",
            latency,
            f"打分稳定性：同一段回答打分 {SCORE_RUNS} 次（temperature={SCORE_TEMPERATURE}，"
            "要求只输出 1–10 的整数）。",
            scoring,
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", action="append", default=[], help="追加候选，如 bailian:qwen3.8-plus"
    )
    args = parser.parse_args()
    print(render(asyncio.run(run(args.model))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
