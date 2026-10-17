"""真实调用结构化输出（make test-live）：前台、评估、备用三个大模型各生成一次 JSON。

验证 response_format=json_object 经 extra_body 传给百炼和 DeepSeek 时能正常工作
（包括开着思考的评估模型），打印重试次数和耗时。未配置的角色跳过。
"""

from __future__ import annotations

import time

import pytest
from pydantic import BaseModel, Field
from test_llm_live import provider_settings

from interview.prompts.loader import PromptLibrary
from interview.prompts.structured import generate_structured
from interview.providers.registry import ProviderNotConfigured, default_factories

PROMPT = """---
id: live_rate
version: 1
description: live 测试用的打分提示词
role: eval
---
<!-- system -->
你是技术面试评估员，按 1–5 分评价回答的技术深度。
<!-- user -->
题目：{{ question }}
{{ answer | as_data("answer") }}
"""


class Rating(BaseModel):
    score: int = Field(ge=1, le=5)
    missing_points: list[str]
    reason: str


@pytest.mark.live
@pytest.mark.parametrize("slot", ["llm_front", "llm_eval", "llm_fallback"])
async def test_structured_output(
    slot: str, tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = provider_settings()
    role = getattr(settings, slot)
    factory = default_factories(settings).llm.get(role.provider)
    if not role.configured or factory is None:
        pytest.skip(f"{slot.upper()}_PROVIDER / {slot.upper()}_MODEL 未配置")
    try:
        llm = factory(role)
    except ProviderNotConfigured as exc:
        pytest.skip(str(exc))
    directory = tmp_path_factory.mktemp("prompts")
    (directory / "live_rate.md").write_text(PROMPT, encoding="utf-8")
    prompt = PromptLibrary(directory).render(
        "live_rate",
        question="线上消息丢失你们是怎么解决的？",
        answer="用本地消息表，业务和消息同一个事务写库，定时任务扫描后投递到 MQ。",
    )
    started = time.perf_counter()
    try:
        result = await generate_structured(llm, prompt, Rating)
    finally:
        await llm.aclose()
    elapsed = time.perf_counter() - started
    assert 1 <= result.value.score <= 5
    with capsys.disabled():
        print(
            f"\n[structured] {slot} {role.provider}/{role.model} 分数 {result.value.score}，"
            f"重试 {result.retries} 次，耗时 {elapsed:.1f} 秒"
        )
