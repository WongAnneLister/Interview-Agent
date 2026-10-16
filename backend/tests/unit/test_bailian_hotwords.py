"""百炼热词表：参数校验、请求体、返回值解析与错误映射（httpx MockTransport）。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from interview.providers.bailian_hotwords import BailianHotwords, Hotword, normalize
from interview.providers.base import (
    AuthError,
    BadResponse,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
)

BASE = "https://api.example.com/api/v1/"


class Recorder:
    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def client(recorder: Recorder) -> BailianHotwords:
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return BailianHotwords(api_key="sk-test", base_url=BASE, timeout_s=5, http_client=http)


def ok(vocabulary_id: str = "vocab-itv-abc") -> httpx.Response:
    return httpx.Response(200, json={"output": {"vocabulary_id": vocabulary_id}, "usage": {}})


def test_normalize_dedupes_and_validates() -> None:
    words = normalize([Hotword(" Kubernetes "), Hotword("Kubernetes", 5), Hotword("向量数据库", 3)])
    assert words == [Hotword("Kubernetes", 4), Hotword("向量数据库", 3)]
    with pytest.raises(ValueError, match="1–5"):
        normalize([Hotword("Redis", 6)])
    with pytest.raises(ValueError, match="15"):
        normalize([Hotword("一" * 16)])
    with pytest.raises(ValueError, match="7"):
        normalize([Hotword("a b c d e f g h")])
    with pytest.raises(ValueError, match="最多 2"):
        normalize([Hotword("a"), Hotword("b"), Hotword("c")], max_words=2)
    with pytest.raises(ValueError, match="为空"):
        normalize([Hotword("  ")])


async def test_create_sends_speech_biasing_request() -> None:
    recorder = Recorder(ok())
    hotwords = client(recorder)
    vocabulary_id = await hotwords.create(
        target_model="fun-asr-realtime",
        prefix="itv01",
        words=[Hotword("LiveKit", 5), Hotword("候选人", lang="zh")],
    )
    await hotwords.aclose()
    assert vocabulary_id == "vocab-itv-abc"
    (request,) = recorder.requests
    assert str(request.url) == "https://api.example.com/api/v1/services/audio/asr/customization"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert json.loads(request.content) == {
        "model": "speech-biasing",
        "input": {
            "action": "create_vocabulary",
            "target_model": "fun-asr-realtime",
            "prefix": "itv01",
            "vocabulary": [
                {"text": "LiveKit", "weight": 5},
                {"text": "候选人", "weight": 4, "lang": "zh"},
            ],
        },
    }


@pytest.mark.parametrize("prefix", ["Interview", "too-long-prefix", "", "a_b"])
async def test_invalid_prefix_is_rejected_before_request(prefix: str) -> None:
    recorder = Recorder(ok())
    with pytest.raises(ValueError, match="prefix"):
        await client(recorder).create(target_model="m", prefix=prefix, words=[Hotword("x")])
    assert recorder.requests == []


async def test_delete_request() -> None:
    recorder = Recorder(httpx.Response(200, json={"output": {}}))
    await client(recorder).delete("vocab-itv-abc")
    assert json.loads(recorder.requests[0].content)["input"] == {
        "action": "delete_vocabulary",
        "vocabulary_id": "vocab-itv-abc",
    }


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(401, json={"code": "InvalidApiKey", "message": "bad key"}), AuthError),
        (httpx.Response(429, json={"code": "Throttling"}), RateLimited),
        (httpx.Response(500, text="oops"), ProviderUnavailable),
        (httpx.Response(400, json={"code": "InvalidParameter", "message": "x"}), ProviderError),
        (httpx.Response(200, json={"output": {}}), BadResponse),
        (httpx.Response(200, text="not json"), BadResponse),
        (httpx.ReadTimeout("slow"), ProviderTimeout),
        (httpx.ConnectError("refused"), ProviderUnavailable),
    ],
)
async def test_error_mapping(response: Any, expected: type[ProviderError]) -> None:
    with pytest.raises(ProviderError) as info:
        await client(Recorder(response)).create(
            target_model="m", prefix="itv", words=[Hotword("x")]
        )
    assert type(info.value) is expected
