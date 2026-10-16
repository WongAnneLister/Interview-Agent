"""OpenAI 兼容适配器：用 MockTransport 模拟流式响应、用量口径、重试与错误映射。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from interview.config import (
    BailianSettings,
    DeepSeekSettings,
    LLMEvalSettings,
    LLMFallbackSettings,
    LLMFrontSettings,
    ProvidersSettings,
    STTSettings,
    TTSSettings,
)
from interview.providers.base import (
    AuthError,
    BadResponse,
    ChatMessage,
    LLMUsage,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
)
from interview.providers.openai_compat import OpenAICompatLLM
from interview.providers.registry import ProviderNotConfigured, default_factories

MESSAGES: list[ChatMessage] = [
    {"role": "system", "content": "你是面试官"},
    {"role": "user", "content": "机密简历正文"},
]
GEN = {"max_tokens": 64, "temperature": 0.3}
BASE_URL = "https://llm.example.com/compatible-mode/v1"

type Handler = Callable[[httpx2.Request], httpx2.Response]


def chunk(delta: dict[str, Any] | None = None, usage: dict[str, Any] | None = None) -> str:
    body: dict[str, Any] = {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    if usage is not None:
        body["usage"] = usage
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


BAILIAN_USAGE = {
    "prompt_tokens": 120,
    "completion_tokens": 30,
    "total_tokens": 150,
    "prompt_tokens_details": {"cached_tokens": 100},
    "completion_tokens_details": {"reasoning_tokens": 10},
}


def sse(*events: str) -> httpx2.Response:
    return httpx2.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=("".join(events) + "data: [DONE]\n\n").encode(),
    )


def ok_stream(usage: dict[str, Any] = BAILIAN_USAGE) -> httpx2.Response:
    return sse(
        chunk({"role": "assistant", "reasoning_content": "先想一想"}),
        chunk({"content": "你好，"}),
        chunk({"content": "请做自我介绍"}),
        chunk(usage=usage),
    )


def error(status: int) -> httpx2.Response:
    return httpx2.Response(status, json={"error": {"message": f"status {status}"}})


class Recorder:
    """按顺序返回预设响应，并记录收到的请求。"""

    def __init__(self, *responses: httpx2.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def make_llm(recorder: Recorder, **kwargs: Any) -> OpenAICompatLLM:
    options: dict[str, Any] = {
        "provider": "bailian",
        "model": "qwen-test",
        "api_key": "sk-test",
        "base_url": BASE_URL,
        "timeout_s": 5,
        "extra_body": {"enable_thinking": False, "seed": 1},
        "retry_base_s": 0,
        **kwargs,
    }
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(recorder))
    return OpenAICompatLLM(**options, http_client=client)


async def test_stream_outputs_content_only_with_usage_and_latency() -> None:
    recorder = Recorder(ok_stream())
    llm = make_llm(recorder)
    async with llm.stream_chat(MESSAGES, **GEN, extra={"seed": 2}) as stream:
        assert [c async for c in stream] == ["你好，", "请做自我介绍"]
    assert stream.usage == LLMUsage(
        prompt_tokens=120, completion_tokens=30, cached_tokens=100, reasoning_tokens=10
    )
    assert stream.first_latency_ms is not None

    request = recorder.requests[0]
    assert str(request.url) == f"{BASE_URL}/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    body = recorder.body()
    assert body["model"] == "qwen-test"
    assert body["messages"] == MESSAGES
    assert (body["max_tokens"], body["temperature"]) == (64, 0.3)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    # 配置的 extra_body 与调用时的 extra 合并，调用时优先
    assert (body["enable_thinking"], body["seed"]) == (False, 2)


async def test_deepseek_cache_hit_tokens() -> None:
    usage = {
        "prompt_tokens": 80,
        "completion_tokens": 5,
        "total_tokens": 85,
        "prompt_cache_hit_tokens": 64,
        "prompt_cache_miss_tokens": 16,
    }
    llm = make_llm(Recorder(ok_stream(usage)), provider="deepseek")
    result = await llm.complete(MESSAGES, **GEN)
    assert result.text == "你好，请做自我介绍"
    assert result.usage == LLMUsage(prompt_tokens=80, completion_tokens=5, cached_tokens=64)
    assert result.first_token_ms is not None


async def test_retries_429_and_5xx_then_succeeds_without_logging_content() -> None:
    recorder = Recorder(error(429), error(503), ok_stream())
    with capture_logs() as logs:
        result = await make_llm(recorder).complete(MESSAGES, **GEN)
    assert result.text == "你好，请做自我介绍"
    assert len(recorder.requests) == 3
    retries = [e for e in logs if e["event"] == "llm_retry"]
    assert [(e["status"], e["attempt"]) for e in retries] == [(429, 1), (503, 2)]
    assert "简历" not in json.dumps(logs, ensure_ascii=False, default=str)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, RateLimited), (500, ProviderUnavailable), (503, ProviderUnavailable)],
)
async def test_gives_up_after_two_retries(status: int, expected: type[ProviderError]) -> None:
    recorder = Recorder(error(status))
    with pytest.raises(expected) as info:
        await make_llm(recorder).complete(MESSAGES, **GEN)
    assert len(recorder.requests) == 3  # 1 次请求 + 2 次重试
    assert (info.value.provider, info.value.model) == ("bailian", "qwen-test")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (error(401), AuthError),
        (error(402), AuthError),
        (error(403), AuthError),
        (error(400), ProviderError),
        (error(422), ProviderError),
        (httpx2.ReadTimeout("slow"), ProviderTimeout),
        (httpx2.ConnectError("refused"), ProviderUnavailable),
    ],
)
async def test_error_mapping_without_retry(
    response: httpx2.Response | Exception, expected: type[ProviderError]
) -> None:
    recorder = Recorder(response)
    stream = make_llm(recorder).stream_chat(MESSAGES, **GEN)
    with pytest.raises(ProviderError) as info:
        await anext(stream)
    assert type(info.value) is expected
    assert len(recorder.requests) == 1
    assert "简历" not in str(info.value)


@pytest.mark.parametrize(
    "bad_event", ["data: {not json\n\n", 'data: {"error": {"message": "x"}}\n\n']
)
async def test_broken_stream_is_bad_response(bad_event: str) -> None:
    response = httpx2.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=(chunk({"content": "你好"}) + bad_event).encode(),
    )
    stream = make_llm(Recorder(response)).stream_chat(MESSAGES, **GEN)
    assert await anext(stream) == "你好"
    with pytest.raises(BadResponse):
        await anext(stream)


# ---- 配置与注册表 ----


def providers(**vendors: Any) -> ProvidersSettings:
    def role(cls: Any, provider: str, model: str) -> Any:
        return cls(provider=provider, model=model)

    return ProvidersSettings(
        llm_front=role(LLMFrontSettings, "bailian", "qwen3.8-flash"),
        llm_eval=role(LLMEvalSettings, "bailian", "qwen3.8-max"),
        llm_fallback=role(LLMFallbackSettings, "deepseek", "deepseek-flash"),
        stt=role(STTSettings, "", ""),
        tts=role(TTSSettings, "", ""),
        **vendors,
    )


def test_role_extra_body_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LLM_FRONT_EXTRA_BODY", "LLM_FALLBACK_EXTRA_BODY", "LLM_EVAL_EXTRA_BODY"):
        monkeypatch.delenv(name, raising=False)
    assert LLMFrontSettings().extra_body == {"enable_thinking": False}
    assert LLMFallbackSettings().extra_body == {"thinking": {"type": "disabled"}}
    assert LLMEvalSettings().extra_body == {}
    monkeypatch.setenv("LLM_FRONT_EXTRA_BODY", '{"enable_thinking": true}')
    assert LLMFrontSettings().extra_body == {"enable_thinking": True}


def test_default_factories_build_vendor_instances() -> None:
    settings = providers(
        bailian=BailianSettings(api_key=SecretStr("sk-b"), base_url=BASE_URL),
        deepseek=DeepSeekSettings(api_key=SecretStr("sk-d"), base_url="https://api.example.com"),
    )
    factories = default_factories(settings)
    front = factories.llm["bailian"](settings.llm_front)
    backup = factories.llm["deepseek"](settings.llm_fallback)
    assert isinstance(front, OpenAICompatLLM)
    assert (front.provider, front.model) == ("bailian", "qwen3.8-flash")
    assert (backup.provider, backup.model) == ("deepseek", "deepseek-flash")


def test_missing_vendor_key_is_not_configured() -> None:
    settings = providers(bailian=BailianSettings(api_key=SecretStr(""), base_url=BASE_URL))
    with pytest.raises(ProviderNotConfigured, match="BAILIAN_API_KEY"):
        default_factories(settings).llm["bailian"](settings.llm_front)
