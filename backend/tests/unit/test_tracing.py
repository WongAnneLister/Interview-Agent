"""链路追踪：属性名与 Langfuse 映射、父子关系、上下文继承、关闭时为空操作。"""

import asyncio
import base64
import json
import threading
from collections.abc import Generator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext, StatusCode
from pydantic import SecretStr

from interview.config import LangfuseSettings, ObservabilitySettings
from interview.observability import tracing
from interview.observability.tracing import (
    Attr,
    init_tracing,
    langfuse_exporter,
    llm_span,
    set_llm_output,
    shutdown_tracing,
    span,
    traced,
)


def setup(*, enabled: bool = True, sample_ratio: float = 1.0) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    init_tracing(
        "api",
        ObservabilitySettings(enabled=enabled, sample_ratio=sample_ratio),
        environment="test",
        exporter=exporter,
        set_global=False,
    )
    return exporter


@pytest.fixture
def exporter() -> Generator[InMemorySpanExporter]:
    yield setup()
    shutdown_tracing()


def by_name(exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    return {s.name: s for s in exporter.get_finished_spans()}


def ctx(s: ReadableSpan) -> SpanContext:
    assert s.context is not None
    return s.context


def test_unified_attributes_and_langfuse_mirrors(exporter: InMemorySpanExporter) -> None:
    with span("turn", interview_id="iv-1", turn_id="t-3", role="front", provider="bailian"):
        pass
    (s,) = exporter.get_finished_spans()
    attrs = dict(s.attributes or {})
    assert attrs[Attr.INTERVIEW_ID] == "iv-1"
    assert attrs[Attr.TURN_ID] == "t-3"
    assert attrs[Attr.AGENT_ROLE] == "front"
    assert attrs[Attr.PROVIDER] == "bailian"
    assert attrs["session.id"] == "iv-1"
    assert attrs["langfuse.observation.type"] == "span"
    assert attrs["langfuse.observation.metadata.agent_role"] == "front"
    assert attrs["langfuse.environment"] == "test"
    assert s.resource.attributes["service.name"] == "api"


def test_parent_child_and_inherited_attributes(exporter: InMemorySpanExporter) -> None:
    other = tracing.tracer_provider().get_tracer("other")
    # 不经过 span() 的 span（如自动埋点）也会带上继承属性
    with (
        span("interview", interview_id="iv-2", is_eval=True),
        span("child"),
        other.start_as_current_span("auto"),
    ):
        pass
    spans = by_name(exporter)
    root, child, auto = spans["interview"], spans["child"], spans["auto"]
    assert child.parent is not None and child.parent.span_id == ctx(root).span_id
    assert auto.parent is not None and auto.parent.span_id == ctx(child).span_id
    assert ctx(child).trace_id == ctx(root).trace_id
    for s in (child, auto):
        attrs = s.attributes or {}
        assert attrs[Attr.INTERVIEW_ID] == "iv-2"
        assert attrs[Attr.IS_EVAL] is True
        assert attrs["langfuse.trace.tags"] == ("eval",)


def test_inherited_attributes_reset_after_block(exporter: InMemorySpanExporter) -> None:
    with span("a", interview_id="iv-3"):
        pass
    with span("b"):
        pass
    assert Attr.INTERVIEW_ID not in (by_name(exporter)["b"].attributes or {})


def test_llm_span_maps_to_langfuse_generation(exporter: InMemorySpanExporter) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with llm_span(
        "llm.front",
        provider="bailian",
        model="qwen-test",
        role="front",
        input=[{"role": "user", "content": "你好"}],
        model_parameters={"temperature": 0.3},
        prompt_name="interviewer",
        prompt_version=2,
    ) as s:
        set_llm_output(
            s, "请介绍一下", usage={"input": 12, "output": 5}, completion_start_time=start
        )
    attrs = dict(exporter.get_finished_spans()[0].attributes or {})
    assert attrs["langfuse.observation.type"] == "generation"
    assert attrs["langfuse.observation.model.name"] == "qwen-test"
    assert attrs["gen_ai.request.model"] == "qwen-test"
    assert attrs[Attr.MODEL] == "qwen-test"
    assert json.loads(str(attrs["langfuse.observation.usage_details"])) == {
        "input": 12,
        "output": 5,
    }
    assert json.loads(str(attrs["langfuse.observation.model.parameters"])) == {"temperature": 0.3}
    assert json.loads(str(attrs["langfuse.observation.input"]))[0]["content"] == "你好"
    assert attrs["langfuse.observation.output"] == "请介绍一下"
    assert attrs["langfuse.observation.prompt.name"] == "interviewer"
    assert attrs["langfuse.observation.prompt.version"] == 2
    assert attrs["langfuse.observation.completion_start_time"] == start.isoformat()


def test_plain_span_with_model_is_not_generation(exporter: InMemorySpanExporter) -> None:
    with span("tts", model="tts-test"):
        pass
    attrs = exporter.get_finished_spans()[0].attributes or {}
    assert attrs["langfuse.observation.type"] == "span"


def test_exception_sets_error_status(exporter: InMemorySpanExporter) -> None:
    with pytest.raises(RuntimeError), span("boom"):
        raise RuntimeError("x")
    (s,) = exporter.get_finished_spans()
    assert s.status.status_code is StatusCode.ERROR
    assert any(e.name == "exception" for e in s.events)


def test_traced_sync_and_async(exporter: InMemorySpanExporter) -> None:
    @traced(role="eval")
    def add(a: int, b: int) -> int:
        return a + b

    @traced("async.work")
    async def work(x: int) -> int:
        await asyncio.sleep(0)
        return x * 2

    assert add(1, 2) == 3
    assert asyncio.run(work(4)) == 8
    assert add.__name__ == "add"
    spans = by_name(exporter)
    assert (spans["test_traced_sync_and_async.<locals>.add"].attributes or {})[
        Attr.AGENT_ROLE
    ] == "eval"
    assert "async.work" in spans


def test_sample_ratio_zero_exports_nothing() -> None:
    exporter = setup(sample_ratio=0.0)
    with span("dropped") as s:
        assert s.get_span_context().is_valid
    assert exporter.get_finished_spans() == ()
    shutdown_tracing()


def test_disabled_is_noop_but_keeps_trace_id() -> None:
    exporter = setup(enabled=False)

    @traced()
    def business() -> str:
        return "ok"

    with span("x", interview_id="iv-4") as s, llm_span("y", provider="p", model="m") as g:
        set_llm_output(g, "out", usage={"input": 1})
        assert not s.is_recording()
        assert s.get_span_context().is_valid
    assert business() == "ok"
    assert exporter.get_finished_spans() == ()
    shutdown_tracing()


def test_tools_are_noop_before_init() -> None:
    shutdown_tracing()
    with span("x") as s:
        assert not s.is_recording()


def test_langfuse_exporter_sends_auth_and_version_headers(exporter: InMemorySpanExporter) -> None:
    """用本机临时 HTTP 服务接收一次真实导出，检查路径和请求头。"""
    received: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received["path"] = self.path
            received["headers"] = dict(self.headers)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    with span("x"):
        pass
    host = f"http://127.0.0.1:{server.server_port}/"
    otlp = langfuse_exporter(
        LangfuseSettings(host=host, public_key="pk-lf-1", secret_key=SecretStr("sk-lf-1"))
    )
    try:
        assert otlp.export(exporter.get_finished_spans()) is SpanExportResult.SUCCESS
    finally:
        otlp.shutdown()
        server.server_close()
    assert received["path"] == "/api/public/otel/v1/traces"
    headers = {k.lower(): v for k, v in received["headers"].items()}
    assert headers["x-langfuse-ingestion-version"] == "4"
    assert headers["authorization"] == "Basic " + base64.b64encode(b"pk-lf-1:sk-lf-1").decode()
    assert headers["content-type"] == "application/x-protobuf"


def test_enabled_without_langfuse_raises() -> None:
    with pytest.raises(ValueError, match="Langfuse"):
        init_tracing("worker", ObservabilitySettings(enabled=True), set_global=False)
    shutdown_tracing()
