"""把厂商适配器的 ProviderError 翻译成 LiveKit Agents 的 APIError。

LiveKit 的识别 / 合成流按 APIError.retryable 决定是否自动重连重试（合成只在还没播出音频时重试），
所以要把“可以再试一次”的错误标成可重试，把鉴权、请求本身有问题的错误标成不可重试。
只翻译类型和厂商 / 型号，不带请求正文（ProviderError 的消息本来也不含正文）。
"""

from __future__ import annotations

from livekit.agents import APIConnectionError, APIError, APITimeoutError

from interview.providers.base import (
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
)


def to_api_error(exc: ProviderError) -> APIError:
    message = str(exc)
    match exc:
        case ProviderTimeout():
            return APITimeoutError(message)
        case ProviderUnavailable() | RateLimited():
            return APIConnectionError(message)
        case _:
            # AuthError、BadResponse、请求被拒：重试也是同样的结果
            return APIError(message, retryable=False)
