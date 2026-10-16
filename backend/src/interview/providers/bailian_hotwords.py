"""百炼预编译热词表（speech-biasing）：创建、删除。热词免费，不记录用量。

接口：POST <BAILIAN_API_BASE_URL>/services/audio/asr/customization
（https://help.aliyun.com/zh/model-studio/vocabulary-http-api ）。
- target_model 必须与识别时用的型号一致；prefix 只能是小写字母和数字，最多 10 位。
- 每个词：weight 1–5；非 ASCII 文本最多 15 个字，纯 ASCII 最多 7 个空格分隔的词。
- fun-asr-realtime 每个表最多 2000 个词；每个账号最多 10 个表（所有型号共享），用完要删除。
后续按 JD 和简历动态生成热词时直接调用这里；tools/create_hotwords.py 是命令行入口。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

import httpx

from interview.observability.tracing import span
from interview.providers.base import (
    AuthError,
    BadResponse,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
)

MAX_WORDS = 2000
MAX_NON_ASCII_CHARS = 15
MAX_ASCII_SEGMENTS = 7
_PREFIX = re.compile(r"^[a-z0-9]{1,10}$")
_PATH = "/services/audio/asr/customization"


@dataclass(frozen=True)
class Hotword:
    text: str
    weight: int = 4
    lang: str | None = None


def normalize(words: Iterable[Hotword], *, max_words: int = MAX_WORDS) -> list[Hotword]:
    """去掉空白和重复（保留第一次出现的），校验权重、长度和数量；不合法时抛 ValueError。"""
    seen: set[str] = set()
    result: list[Hotword] = []
    for word in words:
        text = " ".join(word.text.split())
        if not text or text in seen:
            continue
        if not 1 <= word.weight <= 5:
            raise ValueError(f"热词权重必须在 1–5：{text}={word.weight}")
        if text.isascii():
            if len(text.split(" ")) > MAX_ASCII_SEGMENTS:
                raise ValueError(f"英文热词最多 {MAX_ASCII_SEGMENTS} 个词：{text}")
        elif len(text) > MAX_NON_ASCII_CHARS:
            raise ValueError(f"热词最多 {MAX_NON_ASCII_CHARS} 个字：{text}")
        seen.add(text)
        result.append(Hotword(text, word.weight, word.lang))
    if not result:
        raise ValueError("热词表为空")
    if len(result) > max_words:
        raise ValueError(f"热词最多 {max_words} 个，当前 {len(result)} 个")
    return result


class BailianHotwords:
    provider = "bailian"
    model = "speech-biasing"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_s: float,
        http_client: httpx.AsyncClient | None = None,  # 测试注入
    ) -> None:
        self._client = http_client or httpx.AsyncClient()
        self._url = base_url.rstrip("/") + _PATH
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout_s = timeout_s

    async def create(self, *, target_model: str, prefix: str, words: Iterable[Hotword]) -> str:
        """创建热词表，返回 vocabulary_id。"""
        if not _PREFIX.match(prefix):
            raise ValueError("prefix 只能是小写字母和数字，最多 10 位")
        vocabulary: list[dict[str, Any]] = []
        for word in normalize(words):
            item: dict[str, Any] = {"text": word.text, "weight": word.weight}
            if word.lang:
                item["lang"] = word.lang
            vocabulary.append(item)
        with span("asr.create_vocabulary", provider=self.provider, model=target_model):
            output = await self._call(
                {
                    "action": "create_vocabulary",
                    "target_model": target_model,
                    "prefix": prefix,
                    "vocabulary": vocabulary,
                }
            )
        vocabulary_id = output.get("vocabulary_id")
        if not isinstance(vocabulary_id, str) or not vocabulary_id:
            raise BadResponse("响应缺少 output.vocabulary_id", provider=self.provider)
        return vocabulary_id

    async def delete(self, vocabulary_id: str) -> None:
        with span("asr.delete_vocabulary", provider=self.provider):
            await self._call({"action": "delete_vocabulary", "vocabulary_id": vocabulary_id})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, input: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                self._url,
                json={"model": self.model, "input": input},
                headers=self._headers,
                timeout=self._timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise self._error(ProviderTimeout, "请求超时") from exc
        except httpx.HTTPError as exc:
            raise self._error(ProviderUnavailable, "连接失败") from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        detail = self._detail(body)
        status = response.status_code
        if status in (401, 403):
            raise self._error(AuthError, f"HTTP {status} {detail}")
        if status == 429:
            raise self._error(RateLimited, f"HTTP 429 {detail}")
        if status >= 500:
            raise self._error(ProviderUnavailable, f"HTTP {status} {detail}")
        if status >= 400:
            raise self._error(ProviderError, f"HTTP {status} {detail}")
        output: Any = cast(dict[str, Any], body).get("output") if isinstance(body, dict) else None
        if not isinstance(output, dict):
            raise self._error(BadResponse, "响应缺少 output")
        return cast(dict[str, Any], output)

    @staticmethod
    def _detail(body: Any) -> str:
        if isinstance(body, dict):
            data = cast(dict[str, Any], body)
            return f"{data.get('code', '')} {data.get('message', '')}".strip()
        return ""

    def _error(self, kind: type[ProviderError], message: str) -> ProviderError:
        return kind(f"{self.provider}/{self.model}: {message}", provider=self.provider)
