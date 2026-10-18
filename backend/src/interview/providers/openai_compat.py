"""OpenAI 兼容接口的大模型适配器（openai 官方 SDK 的 AsyncOpenAI）。

百炼和 DeepSeek 各是它的一个配置实例。

- 地址、密钥、型号、超时、extra_body 全部来自配置；extra_body 用于 enable_thinking（百炼）、
  thinking（DeepSeek）等非标准参数，与调用时的 extra 合并（调用时优先）。
- 一律用流式请求（stream_options.include_usage），complete() 也是收齐流再返回，
  这样首 token 耗时和用量的取法一致。
- 只输出正文 delta.content；思考内容 reasoning_content 不输出，只计入用量。
- 429 与 5xx 在收到首个片段之前指数退避重试（SDK 自带的重试关闭，由这里控制并记日志），
  重试用完后按下表抛出，由注册表决定是否切换备用。日志只记录厂商、型号、状态码，不记录正文。

厂商错误 → ProviderError：
  超时 → ProviderTimeout；连接失败、5xx → ProviderUnavailable；429 → RateLimited；
  401 / 402（余额不足）/ 403 → AuthError；响应无法解析 → BadResponse；
  其他 4xx（参数错误、内容审核不通过等，属于请求本身的问题）→ ProviderError。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import Any, cast

import httpx2
import structlog
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AsyncStream,
    InternalServerError,
    OpenAIError,
    RateLimitError,
)
from openai.types.chat import ChatCompletionChunk, ChatCompletionMessageParam
from openai.types.completion_usage import CompletionUsage

from interview.providers.base import (
    AuthError,
    BadResponse,
    ChatMessage,
    GeneratorStream,
    LLMResult,
    LLMStream,
    LLMUsage,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
)

DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BASE_S = 0.5  # 第 n 次重试前等待 base × 2^(n-1) 秒
_AUTH_STATUSES = frozenset({401, 402, 403})

log = structlog.get_logger(__name__)


class OpenAICompatLLM:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key: str,
        base_url: str,
        timeout_s: float,
        extra_body: Mapping[str, Any] | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_s: float = DEFAULT_RETRY_BASE_S,
        http_client: httpx2.AsyncClient | None = None,  # 测试注入
    ) -> None:
        self.provider = provider
        self.model = model
        self._timeout_s = timeout_s
        self._extra_body = dict(extra_body or {})
        self._max_retries = max_retries
        self._retry_base_s = retry_base_s
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_s,
            max_retries=0,
            http_client=http_client,
        )

    def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> LLMStream:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": cast(list[ChatCompletionMessageParam], [dict(m) for m in messages]),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
            "timeout": timeout_s or self._timeout_s,
            "extra_body": {**self._extra_body, **(extra or {})},
        }

        async def body(stream: GeneratorStream[str, LLMUsage]) -> AsyncGenerator[str]:
            stream.mark_start()
            response = await self._create(params)
            try:
                async for chunk in response:
                    if chunk.usage is not None:
                        stream.usage = to_usage(chunk.usage)
                    for choice in chunk.choices:
                        if choice.delta.content:
                            stream.mark_first()
                            yield choice.delta.content
            except (OpenAIError, httpx2.HTTPError, ValueError) as exc:  # ValueError：数据不是 JSON
                raise self._map_error(exc) from exc
            finally:
                await response.close()

        return GeneratorStream(body)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> LLMResult:
        async with self.stream_chat(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_s=timeout_s,
            extra=extra,
        ) as stream:
            text = "".join([chunk async for chunk in stream])
        return LLMResult(
            text=text,
            usage=stream.usage,
            first_token_ms=stream.first_latency_ms,
            provider=self.provider,
            model=self.model,
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def _create(self, params: dict[str, Any]) -> AsyncStream[ChatCompletionChunk]:
        """发出请求并等到响应头；429 / 5xx 退避重试，其余错误直接映射后抛出。"""
        attempt = 0
        while True:
            try:
                return cast(
                    AsyncStream[ChatCompletionChunk],
                    await self._client.chat.completions.create(**params),
                )
            except (RateLimitError, InternalServerError) as exc:
                if attempt >= self._max_retries:
                    raise self._map_error(exc) from exc
                attempt += 1
                delay = self._retry_base_s * 2 ** (attempt - 1)
                log.warning(
                    "llm_retry",
                    provider=self.provider,
                    model=self.model,
                    status=exc.status_code,
                    attempt=attempt,
                    delay_s=delay,
                )
                await asyncio.sleep(delay)
            except (OpenAIError, httpx2.HTTPError) as exc:
                raise self._map_error(exc) from exc

    def _map_error(self, exc: Exception) -> ProviderError:
        kind: type[ProviderError]
        match exc:
            case APITimeoutError() | httpx2.TimeoutException():
                kind, message = ProviderTimeout, "请求超时"
            case APIConnectionError() | httpx2.TransportError():
                kind, message = ProviderUnavailable, "连接失败或中断"
            case APIResponseValidationError():
                kind, message = BadResponse, "响应格式不符合预期"
            case RateLimitError():
                kind, message = RateLimited, "HTTP 429 限流"
            case APIStatusError(status_code=status) if status >= 500:
                kind, message = ProviderUnavailable, f"HTTP {status}"
            case APIStatusError(status_code=status) if status in _AUTH_STATUSES:
                kind, message = AuthError, f"HTTP {status} 鉴权失败、余额不足或无权限"
            case APIStatusError(status_code=status):
                kind, message = ProviderError, f"HTTP {status} 请求被拒绝"
            case _:
                # 流中途收到的错误事件、无法解析的数据等
                kind, message = BadResponse, type(exc).__name__
        return kind(
            f"{self.provider}/{self.model}: {message}", provider=self.provider, model=self.model
        )


def to_usage(usage: CompletionUsage) -> LLMUsage:
    """统一成“prompt 含缓存命中、completion 含思考”的口径。

    缓存命中：百炼在 prompt_tokens_details.cached_tokens；DeepSeek 在顶层 prompt_cache_hit_tokens。
    """
    cached = usage.prompt_tokens_details.cached_tokens if usage.prompt_tokens_details else None
    if cached is None:
        cached = (usage.model_extra or {}).get("prompt_cache_hit_tokens")
    details = usage.completion_tokens_details
    reasoning = details.reasoning_tokens if details else None
    return LLMUsage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cached_tokens=int(cached or 0),
        reasoning_tokens=reasoning or 0,
    )
