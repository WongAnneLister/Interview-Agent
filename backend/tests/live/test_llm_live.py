"""真实调用大模型（make test-live）：流式拿到回答，打印首 token 耗时和用量。

直接用适配器调用，不经过观测包装，不写用量表，不需要隧道；需要 .env 里配置好对应角色和厂商密钥，
未配置的角色跳过。
"""

from __future__ import annotations

from typing import Any

import pytest

from interview.config import (
    DEFAULT_ENV_FILE,
    BailianSettings,
    DeepSeekSettings,
    LLMEvalSettings,
    LLMFallbackSettings,
    LLMFrontSettings,
    ProvidersSettings,
)
from interview.providers.base import ChatMessage
from interview.providers.registry import ProviderNotConfigured, default_factories

MESSAGES: list[ChatMessage] = [
    {"role": "system", "content": "你是技术面试官，用一句话回答。"},
    {"role": "user", "content": "请用一句话介绍你自己。"},
]


def provider_settings() -> ProvidersSettings:
    """只读取大模型相关配置：live 测试不需要数据库、LiveKit、对象存储等其他必填项。"""

    def read(cls: type[Any]) -> Any:
        return cls(_env_file=DEFAULT_ENV_FILE)

    return ProvidersSettings.model_construct(
        llm_front=read(LLMFrontSettings),
        llm_eval=read(LLMEvalSettings),
        llm_fallback=read(LLMFallbackSettings),
        bailian=read(BailianSettings),
        deepseek=read(DeepSeekSettings),
    )


@pytest.mark.live
@pytest.mark.parametrize("slot", ["llm_front", "llm_eval", "llm_fallback"])
async def test_stream_chat(slot: str, capsys: pytest.CaptureFixture[str]) -> None:
    settings = provider_settings()
    role = getattr(settings, slot)
    factory = default_factories(settings).llm.get(role.provider)
    if not role.configured or factory is None:
        pytest.skip(f"{slot.upper()}_PROVIDER / {slot.upper()}_MODEL 未配置")
    try:
        llm = factory(role)
    except ProviderNotConfigured as exc:
        pytest.skip(str(exc))
    try:
        async with llm.stream_chat(MESSAGES, max_tokens=200, temperature=0.3) as stream:
            text = "".join([chunk async for chunk in stream])
    finally:
        await llm.aclose()

    assert text.strip()
    assert stream.first_latency_ms is not None
    assert stream.usage is not None and stream.usage.completion_tokens > 0
    with capsys.disabled():
        print(
            f"\n[{slot}] {role.provider}/{role.model} 首 token {stream.first_latency_ms:.0f} ms，"
            f"用量 {stream.usage}\n  回答：{text}"
        )
