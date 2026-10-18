"""JD 解析的模型校验、重试与预览 API，不访问数据库或真实厂商。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError
from structlog.testing import capture_logs
from test_api import USER, error_of, fake_resources, make_settings, open_client
from test_api import exporter as exporter

from interview.api.app import create_app
from interview.api.deps import get_jd_llm, routes_without_user
from interview.db.models import UsageRole
from interview.domain.jd import JDParsed, JDText, parse_jd
from interview.observability import usage
from interview.prompts.loader import DATA_NOTICE
from interview.prompts.structured import StructuredOutputError
from interview.providers.base import LLMProvider, RateLimited
from interview.providers.fake import FakeLLM
from interview.providers.registry import ProviderFactories, ProviderRegistry

JD = "高级后端工程师：熟悉 Go、PostgreSQL、Redis，有分布式系统设计和团队协作经验。"


def payload(count: int = 3) -> dict[str, Any]:
    return {
        "title": "后端工程师",
        "level": "高级",
        "competencies": [
            {
                "name": f"能力{i}",
                "description": "系统设计能力",
                "weight": i + 1,
                "probes": ["服务拆分", "故障恢复"],
            }
            for i in range(count)
        ],
        "tech_terms": ["Go", "PostgreSQL", "Redis"],
    }


@pytest.mark.parametrize("count", [3, 4, 5, 6])
def test_weights_normalize_without_mutating_input(count: int) -> None:
    data = payload(count)
    original = deepcopy(data)
    parsed = JDParsed.model_validate(data)
    assert len(parsed.competencies) == count
    assert sum(c.weight for c in parsed.competencies) == pytest.approx(1)
    assert parsed.competencies[-1].weight / parsed.competencies[0].weight == pytest.approx(count)
    assert data == original
    assert JDParsed.model_validate(parsed.model_dump()).model_dump() == parsed.model_dump()


@pytest.mark.parametrize("weights", [[1e308] * 3, [1e-300] * 3, [0, 2, 3]])
def test_normalization_handles_large_small_and_zero_weights(weights: list[float]) -> None:
    data = payload()
    for item, weight in zip(data["competencies"], weights, strict=True):
        item["weight"] = weight
    parsed = JDParsed.model_validate(data)
    assert sum(c.weight for c in parsed.competencies) == pytest.approx(1)
    assert all(0 <= c.weight <= 1 for c in parsed.competencies)


@pytest.mark.parametrize("count", [0, 2, 7])
def test_rejects_wrong_competency_count(count: int) -> None:
    with pytest.raises(ValidationError):
        JDParsed.model_validate(payload(count))


@pytest.mark.parametrize("weight", [-1, float("nan"), float("inf"), "large", None])
def test_rejects_invalid_weights(weight: object) -> None:
    data = payload()
    data["competencies"][0]["weight"] = weight
    with pytest.raises(ValidationError):
        JDParsed.model_validate(data)


def test_rejects_all_zero_weights() -> None:
    data = payload()
    for item in data["competencies"]:
        item["weight"] = 0
    with pytest.raises(ValidationError):
        JDParsed.model_validate(data)


def test_terms_are_cleaned_deduplicated_and_bounded() -> None:
    data = payload()
    data["tech_terms"] = [
        " Go ",
        "go",
        "",
        " \t ",
        "SQL  Server",
        "sql server",
        "x" * 21,
        "x" * 20,
        "C",
        "C++",
        "C#",
    ]
    data["tech_terms"] += [f"Term{i}" for i in range(40)]
    terms = JDParsed.model_validate(data).tech_terms
    assert terms[:6] == ["Go", "SQL Server", "x" * 20, "C", "C++", "C#"]
    assert len(terms) == 30
    assert all(0 < len(t) <= 20 for t in terms)


@pytest.mark.parametrize("text", ["", " \n\t ", "字" * 10001, "字" * 10000 + " "])
async def test_invalid_input_never_calls_llm(text: str) -> None:
    llm = FakeLLM(reply=json.dumps(payload()))
    with pytest.raises(ValidationError):
        await parse_jd(llm, text)
    assert llm.calls == []


async def test_prompt_and_input_boundary() -> None:
    text = "字" * 10000
    assert JDText(text=text).text == text
    llm = FakeLLM(reply=json.dumps(payload()))
    result = await parse_jd(llm, text)
    assert (result.prompt_id, result.prompt_version, result.retries) == ("jd_parse", 1, 0)
    system, user = llm.calls[0]["messages"]
    assert DATA_NOTICE in system["content"]
    assert f'<user_data type="jd">\n{text}\n</user_data>' == user["content"]


@pytest.mark.parametrize(
    "bad",
    [
        "not JSON",
        json.dumps(payload(2)),
        json.dumps({**payload(), "competencies": ["wrong"] * 3}),
        json.dumps({**payload(), "tech_terms": [None]}),
    ],
)
async def test_invalid_model_output_is_retried(bad: str) -> None:
    replies = iter([bad, json.dumps(payload())])
    llm = FakeLLM(reply=lambda _messages: next(replies))
    result = await parse_jd(llm, JD)
    assert result.retries == 1 and len(llm.calls) == 2
    assert llm.calls[1]["messages"][-2]["content"] == bad


async def test_retry_limit_and_logs_hide_text() -> None:
    llm = FakeLLM(reply="PRIVATE_MODEL_OUTPUT")
    with capture_logs() as logs, pytest.raises(StructuredOutputError) as caught:
        await parse_jd(llm, "PRIVATE_JD_TEXT")
    assert caught.value.attempts == len(llm.calls) == 3
    assert "PRIVATE" not in json.dumps(logs)


@pytest.fixture
async def api() -> AsyncIterator[tuple[httpx.AsyncClient, FakeLLM]]:
    llm = FakeLLM(reply=json.dumps(payload()))
    app = create_app(make_settings(), resource_factory=fake_resources(), init_observability=False)

    def provider() -> LLMProvider:
        return llm

    app.dependency_overrides[get_jd_llm] = provider
    assert routes_without_user(app) == []
    async with open_client(app) as client:
        yield client, llm


async def test_api_returns_preview(api: tuple[httpx.AsyncClient, FakeLLM]) -> None:
    client, _llm = api
    response = await client.post("/api/jd/parse", json={"text": JD})
    assert response.status_code == 200
    assert response.json() == JDParsed.model_validate(payload()).model_dump()


@pytest.mark.parametrize("text", ["", "  ", "字" * 10001])
async def test_api_rejects_invalid_input(
    api: tuple[httpx.AsyncClient, FakeLLM],
    text: str,
) -> None:
    client, llm = api
    response = await client.post("/api/jd/parse", json={"text": text})
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_error"
    assert llm.calls == []


async def test_api_exhausted_retries(api: tuple[httpx.AsyncClient, FakeLLM]) -> None:
    client, llm = api
    llm._reply = "PRIVATE_MODEL_OUTPUT"
    response = await client.post("/api/jd/parse", json={"text": JD})
    assert response.status_code == 502
    assert error_of(response)["code"] == "parse_failed"
    assert "PRIVATE" not in response.text and len(llm.calls) == 3


async def test_api_model_unavailable(api: tuple[httpx.AsyncClient, FakeLLM]) -> None:
    client, llm = api
    llm._errors.append(RateLimited("429", provider="fake", model="m"))
    response = await client.post("/api/jd/parse", json={"text": JD})
    assert response.status_code == 503
    assert error_of(response)["code"] == "model_unavailable"


@pytest.mark.parametrize("user", [USER, None])
async def test_api_requires_user_and_configured_model(user: Any) -> None:
    app = create_app(
        make_settings(user=user), resource_factory=fake_resources(), init_observability=False
    )
    async with open_client(app) as client:
        response = await client.post("/api/jd/parse", json={"text": JD})
    assert response.status_code == 503
    assert error_of(response)["code"] == (
        "user_not_configured" if user is None else "service_unavailable"
    )


async def test_jd_span_and_retry_usage(
    exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[dict[str, Any]] = []

    async def record(**kwargs: Any) -> None:
        records.append(kwargs)

    monkeypatch.setattr(usage, "record_usage", record)
    settings = make_settings().providers
    settings.llm_eval.provider, settings.llm_eval.model = "fake", "test-jd"
    replies = iter(["not JSON", json.dumps(payload())])
    llm = FakeLLM(reply=lambda _messages: next(replies))
    registry = ProviderRegistry(settings, factories=ProviderFactories(llm={"fake": lambda _: llm}))
    try:
        await parse_jd(get_jd_llm(USER, registry), JD)
    finally:
        await registry.aclose()
    assert [r["is_retry"] for r in records] == [False, True]
    assert all(r["role"] == UsageRole.JD_PARSE and r["user_id"] == USER for r in records)
    attrs = next(s.attributes for s in exporter.get_finished_spans() if s.name == "jd.parse")
    assert attrs is not None
    assert attrs["prompt.id"] == "jd_parse" and attrs["prompt.version"] == 1
    assert attrs["agent.role"] == "jd_parse" and attrs["structured.retries"] == 1
