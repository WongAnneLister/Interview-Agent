"""自动埋点：FastAPI 服务端、httpx 客户端、SQLAlchemy 产生 span；关闭时不安装。"""

import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
import pytest
import sqlalchemy
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from interview.config import ObservabilitySettings
from interview.observability.instrument import (
    instrument_fastapi,
    instrument_httpx,
    instrument_sqlalchemy,
    uninstrument_all,
)
from interview.observability.tracing import (
    Attr,
    init_tracing,
    shutdown_tracing,
    span,
    tracer_provider,
)


@pytest.fixture
def exporter() -> Generator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    init_tracing("api", ObservabilitySettings(enabled=True), exporter=exporter, set_global=False)
    yield exporter
    uninstrument_all()
    shutdown_tracing()


def make_app() -> FastAPI:
    app = FastAPI()

    @app.get("/hello")
    def hello() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        with span("handler"):
            return {"ok": "1"}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {}

    return app


def spans_of_kind(exporter: InMemorySpanExporter, kind: SpanKind) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if s.kind is kind]


def test_fastapi_server_span_is_parent_of_handler(exporter: InMemorySpanExporter) -> None:
    app = make_app()
    instrument_fastapi(app, enabled=True)
    with TestClient(app) as client:
        assert client.get("/hello").status_code == 200
        client.get("/healthz")
    (server,) = spans_of_kind(exporter, SpanKind.SERVER)
    assert server.name == "GET /hello"  # 健康检查不产生 span
    handler = next(s for s in exporter.get_finished_spans() if s.name == "handler")
    assert server.context is not None and handler.parent is not None
    assert handler.parent.span_id == server.context.span_id


async def test_trace_propagates_from_httpx_to_fastapi(exporter: InMemorySpanExporter) -> None:
    app = make_app()
    instrument_fastapi(app, enabled=True)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    HTTPXClientInstrumentor.instrument_client(client, tracer_provider=tracer_provider())
    async with client:
        with span("root", interview_id="iv-1") as root:
            await client.get("/hello")
    trace_id = root.get_span_context().trace_id
    spans = exporter.get_finished_spans()
    assert {s.context.trace_id for s in spans if s.context is not None} == {trace_id}
    (client_span,) = spans_of_kind(exporter, SpanKind.CLIENT)
    assert (client_span.attributes or {})[Attr.INTERVIEW_ID] == "iv-1"
    # 同进程内调用时服务端 span 是 INTERNAL（已有当前 span）；跨进程时为 SERVER，经 traceparent 关联
    server = next(s for s in spans if s.name == "GET /hello")
    assert client_span.context is not None and server.parent is not None
    assert server.parent.span_id == client_span.context.span_id


def test_httpx_global_instrumentation(exporter: InMemorySpanExporter) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    instrument_httpx(enabled=True)
    try:
        with span("call", interview_id="iv-2"):
            httpx.get(f"http://127.0.0.1:{server.server_port}/ping", timeout=5)
    finally:
        server.server_close()
    (client_span,) = spans_of_kind(exporter, SpanKind.CLIENT)
    assert (client_span.attributes or {})[Attr.INTERVIEW_ID] == "iv-2"


def test_sqlalchemy_span(exporter: InMemorySpanExporter) -> None:
    engine = sqlalchemy.create_engine("sqlite://")
    instrument_sqlalchemy(engine, enabled=True)
    with span("query", interview_id="iv-3"), engine.connect() as conn:
        conn.execute(sqlalchemy.text("SELECT 1"))
    db_spans = spans_of_kind(exporter, SpanKind.CLIENT)
    assert db_spans
    assert (db_spans[0].attributes or {})[Attr.INTERVIEW_ID] == "iv-3"


def test_disabled_installs_nothing(exporter: InMemorySpanExporter) -> None:
    app = make_app()
    engine = sqlalchemy.create_engine("sqlite://")
    instrument_fastapi(app, enabled=False)
    instrument_httpx(enabled=False)
    instrument_sqlalchemy(engine, enabled=False)
    with TestClient(app) as client:
        client.get("/hello")
    with engine.connect() as conn:
        conn.execute(sqlalchemy.text("SELECT 1"))
    assert not getattr(app, "_is_instrumented_by_opentelemetry", False)
    assert not HTTPXClientInstrumentor().is_instrumented_by_opentelemetry
    assert [s.name for s in exporter.get_finished_spans()] == ["handler"]
