"""API 骨架（httpx.AsyncClient）。

健康检查、请求 ID 与 trace_id、访问日志、统一错误格式、用户依赖、CORS。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import format_trace_id
from structlog.contextvars import get_contextvars
from structlog.testing import capture_logs

from interview.api.app import create_app
from interview.api.deps import CurrentUserId, routes_without_user
from interview.api.errors import NotFoundError
from interview.api.middleware import resolve_request_id
from interview.api.resources import Resources
from interview.config import (
    AppSettings,
    CostSettings,
    DatabaseSettings,
    LangfuseSettings,
    LiveKitSettings,
    MetricsSettings,
    ObservabilitySettings,
    ProvidersSettings,
    RedisSettings,
    S3Settings,
    Settings,
)
from interview.observability.tracing import init_tracing, shutdown_tracing
from interview.providers.registry import ProviderRegistry

USER = uuid.uuid4()
WEB = "http://localhost:3000"


def make_settings(*, user: uuid.UUID | None = USER, observability: bool = False) -> Settings:
    def build(cls: Any, **kwargs: Any) -> Any:
        return cls(**kwargs)

    empty = {"provider": "", "model": ""}
    return Settings(
        app=build(AppSettings, default_user_id=user, web_origins=[WEB]),
        db=build(DatabaseSettings, url="postgresql+asyncpg://u:p@127.0.0.1:1/db"),
        redis=build(RedisSettings, url="redis://127.0.0.1:1/0"),
        livekit=build(LiveKitSettings, url="ws://x", api_key="k", api_secret="s"),
        s3=build(S3Settings, endpoint="http://x", access_key="a", secret_key="s"),
        providers=ProvidersSettings.model_validate(
            {k: empty for k in ("llm_front", "llm_eval", "llm_fallback", "stt", "tts")}
        ),
        observability=ObservabilitySettings(enabled=observability),
        langfuse=build(LangfuseSettings),
        metrics=build(MetricsSettings),
        cost=build(CostSettings),
    )


async def ok() -> None:
    return None


async def broken() -> None:
    raise ConnectionRefusedError("127.0.0.1:6379 拒绝连接")


async def slow() -> None:
    await asyncio.sleep(10)


def fake_resources(
    checks: dict[str, Callable[[], Any]] | None = None,
) -> Callable[[Settings], Resources]:
    def factory(settings: Settings) -> Resources:
        return Resources(
            settings=settings,
            sessionmaker=cast(Any, None),
            providers=ProviderRegistry(settings.providers),
            ready_checks=checks or {"database": ok, "redis": ok},
        )

    return factory


def add_test_routes(app: FastAPI) -> None:
    async def me(user_id: CurrentUserId) -> dict[str, str]:
        return {"user_id": str(user_id), "trace_id": get_contextvars()["trace_id"]}

    async def missing(user_id: CurrentUserId) -> None:
        raise NotFoundError("面试不存在")

    async def boom(user_id: CurrentUserId) -> None:
        raise RuntimeError("数据库密码是 hunter2")

    async def item(n: int, user_id: CurrentUserId) -> dict[str, int]:
        return {"n": n}

    app.add_api_route("/test/me", me, methods=["GET"])
    app.add_api_route("/test/missing", missing, methods=["GET"])
    app.add_api_route("/test/boom", boom, methods=["GET"])
    app.add_api_route("/test/items/{n}", item, methods=["GET"])


@asynccontextmanager
async def open_client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """运行应用的 lifespan，并返回直连应用的客户端。"""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(make_settings(), resource_factory=fake_resources(), init_observability=False)
    add_test_routes(app)
    async with open_client(app) as c:
        yield c


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    exp = InMemorySpanExporter()
    init_tracing(
        "api",
        ObservabilitySettings(enabled=True),
        environment="test",
        exporter=exp,
        set_global=False,
    )
    yield exp
    shutdown_tracing()


def error_of(response: httpx.Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "trace_id"}
    assert body["error"]["trace_id"] == response.headers["x-request-id"]
    return body["error"]


# ---- 健康检查 ----


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})
    assert len(response.headers["x-request-id"]) == 32


async def test_readyz_all_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "redis": "ok"}}


async def test_readyz_reports_failures_without_details() -> None:
    settings = make_settings()
    settings.app.ready_check_timeout_s = 0.05
    factory = fake_resources({"database": slow, "redis": broken})
    app = create_app(settings, resource_factory=factory, init_observability=False)
    async with open_client(app) as c:
        response = await c.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "timeout", "redis": "ConnectionRefusedError"},
    }
    assert "6379" not in response.text


# ---- 请求 ID、trace_id 与访问日志 ----


def test_resolve_request_id() -> None:
    valid = uuid.uuid4().hex
    assert resolve_request_id(valid.upper()) == (valid, None)
    generated, original = resolve_request_id("abc-123\n")
    assert len(generated) == 32 and original == "abc-123"
    assert resolve_request_id("0" * 32)[1] == "0" * 32  # 全 0 不是合法 trace_id
    assert resolve_request_id(None)[1] is None


async def test_request_id_is_passed_through_and_bound_to_logs(client: httpx.AsyncClient) -> None:
    request_id = uuid.uuid4().hex
    with capture_logs() as logs:
        response = await client.get("/test/me", headers={"X-Request-ID": request_id})
    assert response.headers["x-request-id"] == request_id
    assert response.json() == {"user_id": str(USER), "trace_id": request_id}
    (access,) = [e for e in logs if e["event"] == "http_request"]
    assert (access["method"], access["path"], access["status"]) == ("GET", "/test/me", 200)
    assert access["duration_ms"] >= 0


async def test_invalid_request_id_is_replaced(client: httpx.AsyncClient) -> None:
    response = await client.get("/test/me", headers={"X-Request-ID": "not-a-trace-id"})
    assert response.headers["x-request-id"] != "not-a-trace-id"
    assert response.json()["trace_id"] == response.headers["x-request-id"]


async def test_health_checks_are_not_access_logged(client: httpx.AsyncClient) -> None:
    with capture_logs() as logs:
        await client.get("/healthz")
        await client.get("/readyz")
    assert [e for e in logs if e["event"] == "http_request"] == []


async def test_request_id_becomes_otel_trace_id(exporter: InMemorySpanExporter) -> None:
    app = create_app(
        make_settings(observability=True),
        resource_factory=fake_resources(),
        init_observability=False,
    )
    add_test_routes(app)
    request_id = uuid.uuid4().hex
    async with open_client(app) as c:
        await c.get("/test/me", headers={"X-Request-ID": request_id})
    trace_ids = {
        format_trace_id(s.context.trace_id) for s in exporter.get_finished_spans() if s.context
    }
    assert trace_ids == {request_id}


# ---- 统一错误格式 ----


async def test_app_error(client: httpx.AsyncClient) -> None:
    response = await client.get("/test/missing")
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"
    assert error_of(response)["message"] == "面试不存在"


async def test_unhandled_error_hides_details_and_logs_stack(client: httpx.AsyncClient) -> None:
    with capture_logs() as logs:
        response = await client.get("/test/boom")
    assert response.status_code == 500
    error = error_of(response)
    assert error["code"] == "internal_error"
    assert "hunter2" not in response.text
    assert "Traceback" not in response.text
    (logged,) = [e for e in logs if e["event"] == "http_unhandled_error"]
    assert logged["exc_info"] is True
    (access,) = [e for e in logs if e["event"] == "http_request"]
    assert (access["status"], access["log_level"]) == (500, "error")


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/nope", 404, "not_found"),
        ("POST", "/healthz", 405, "method_not_allowed"),
        ("GET", "/test/items/abc", 422, "validation_error"),
    ],
)
async def test_framework_errors_use_same_format(
    client: httpx.AsyncClient, method: str, path: str, status: int, code: str
) -> None:
    response = await client.request(method, path)
    assert response.status_code == status
    assert error_of(response)["code"] == code
    if status == 404:
        assert error_of(response)["message"] == "接口不存在"
    if status == 422:
        assert "abc" not in error_of(response)["message"]  # 不回显输入值


# ---- 用户依赖 ----


async def test_missing_default_user_is_503() -> None:
    app = create_app(
        make_settings(user=None), resource_factory=fake_resources(), init_observability=False
    )
    add_test_routes(app)
    async with open_client(app) as c:
        response = await c.get("/test/me")
    assert response.status_code == 503
    assert error_of(response)["code"] == "user_not_configured"


def test_every_business_route_requires_user() -> None:
    app = create_app(make_settings(), resource_factory=fake_resources(), init_observability=False)
    assert routes_without_user(app) == []
    add_test_routes(app)
    assert routes_without_user(app) == []

    async def leaky() -> None:
        return None

    app.add_api_route("/leaky", leaky, methods=["GET"])
    assert routes_without_user(app) == ["['GET'] /leaky"]


# ---- CORS ----


async def test_cors_only_allows_configured_origin(client: httpx.AsyncClient) -> None:
    preflight = {"Access-Control-Request-Method": "GET"}
    allowed = await client.options("/test/me", headers={"Origin": WEB, **preflight})
    assert allowed.headers["access-control-allow-origin"] == WEB
    denied = await client.options(
        "/test/me", headers={"Origin": "https://evil.example", **preflight}
    )
    assert "access-control-allow-origin" not in denied.headers
    simple = await client.get("/test/me", headers={"Origin": WEB})
    assert "x-request-id" in simple.headers["access-control-expose-headers"].lower()
