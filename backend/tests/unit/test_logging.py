"""结构化日志：JSON 输出、取自 OpenTelemetry 的 trace_id / span_id、interview_id 上下文、脱敏。"""

import asyncio
import io
import json
from collections.abc import Generator
from typing import Any

import pytest
import structlog
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import format_span_id, format_trace_id

from interview.config import ObservabilitySettings
from interview.logging import configure_logging, log_context, redact
from interview.observability.tracing import init_tracing, shutdown_tracing, span


@pytest.fixture(autouse=True, params=[True, False], ids=["tracing-on", "tracing-off"])
def tracing(request: pytest.FixtureRequest) -> Generator[None]:
    """开启和关闭可观测性时，日志里的 trace_id 行为一致。"""
    init_tracing(
        "api",
        ObservabilitySettings(enabled=request.param),
        exporter=InMemorySpanExporter(),
        set_global=False,
    )
    yield
    shutdown_tracing()


def capture() -> io.StringIO:
    stream = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=stream)
    return stream


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_output_has_otel_ids_and_interview_id() -> None:
    stream = capture()
    with span("turn") as current, log_context(interview_id="iv-1"):
        structlog.get_logger().info("turn_started", seq=3)
    structlog.get_logger().info("outside")

    inside, outside = lines(stream)
    ctx = current.get_span_context()
    assert inside["event"] == "turn_started"
    assert inside["trace_id"] == format_trace_id(ctx.trace_id)
    assert inside["span_id"] == format_span_id(ctx.span_id)
    assert len(inside["trace_id"]) == 32
    assert inside["interview_id"] == "iv-1"
    assert inside["level"] == "info"
    assert "timestamp" in inside
    assert "trace_id" not in outside
    assert "span_id" not in outside


def test_child_span_shares_trace_id() -> None:
    stream = capture()
    with span("parent"):
        structlog.get_logger().info("a")
        with span("child"):
            structlog.get_logger().info("b")
    a, b = lines(stream)
    assert a["trace_id"] == b["trace_id"]
    assert a["span_id"] != b["span_id"]


def test_context_is_isolated_between_tasks() -> None:
    stream = capture()

    async def worker(interview_id: str) -> None:
        with span(interview_id), log_context(interview_id=interview_id):
            await asyncio.sleep(0)
            structlog.get_logger().info("tick")

    async def main() -> None:
        await asyncio.gather(worker("a"), worker("b"))

    asyncio.run(main())
    records = lines(stream)
    assert {r["interview_id"] for r in records} == {"a", "b"}
    assert records[0]["trace_id"] != records[1]["trace_id"]


def test_redaction_in_output() -> None:
    stream = capture()
    structlog.get_logger().info(
        "call",
        api_key="sk-123",
        LIVEKIT_API_SECRET="s",
        access_token="t",
        db_password="p",
        resume_text="张三，十年经验",
        transcript=["你好", "请介绍一下"],
        input_tokens=120,
        headers={"x-api-key": "sk-456", "nested": [{"refresh_token": "r"}]},
    )
    (record,) = lines(stream)
    assert record["api_key"] == "***"
    assert record["LIVEKIT_API_SECRET"] == "***"
    assert record["access_token"] == "***"
    assert record["db_password"] == "***"
    assert record["resume_text_len"] == 7
    assert record["transcript_len"] == 2
    assert "resume_text" not in record
    assert "transcript" not in record
    assert record["input_tokens"] == 120
    assert record["headers"]["x-api-key"] == "***"
    assert record["headers"]["nested"][0]["refresh_token"] == "***"
    raw = stream.getvalue()
    for secret in ("sk-123", "sk-456", "张三", "请介绍"):
        assert secret not in raw


def test_redaction_applies_to_context_vars() -> None:
    stream = capture()
    with log_context(token="ctx-secret"):
        structlog.get_logger().info("x")
    assert "ctx-secret" not in stream.getvalue()


def test_redact_processor_directly() -> None:
    out = redact(None, "info", {"event": "e", "password": "p", "resume_text": "abc"})
    assert out == {"event": "e", "password": "***", "resume_text_len": 3}


def test_console_format_renders() -> None:
    stream = io.StringIO()
    configure_logging(fmt="console", stream=stream)
    with span("x") as current:
        structlog.get_logger().info("hello", api_key="sk-789")
    text = stream.getvalue()
    assert "hello" in text
    assert format_trace_id(current.get_span_context().trace_id) in text
    assert "sk-789" not in text


def test_level_filtering() -> None:
    stream = io.StringIO()
    configure_logging(level="WARNING", stream=stream)
    structlog.get_logger().info("dropped")
    structlog.get_logger().warning("kept")
    assert [r["event"] for r in lines(stream)] == ["kept"]


@pytest.mark.parametrize("fmt", ["json", "console"])
def test_exception_chain_retains_locations_without_sensitive_values(fmt: Any) -> None:
    stream = io.StringIO()
    configure_logging(fmt=fmt, stream=stream)
    try:
        try:
            raise ValueError("PRIVATE_SQL_PARAMETER")
        except ValueError as exc:
            raise RuntimeError("PRIVATE_RESUME_CONTENT") from exc
    except RuntimeError:
        structlog.get_logger().exception("failed")
    output = stream.getvalue()
    assert "PRIVATE_SQL_PARAMETER" not in output
    assert "PRIVATE_RESUME_CONTENT" not in output
    assert "ValueError" in output and "RuntimeError" in output
    assert "test_logging.py" in output


def test_exception_group_does_not_expose_messages() -> None:
    stream = capture()
    try:
        raise ExceptionGroup("PRIVATE_GROUP", [ValueError("PRIVATE_CHILD")])
    except ExceptionGroup:
        structlog.get_logger().exception("failed_group")
    record = lines(stream)[0]
    assert "ExceptionGroup" in record["exception"] and "ValueError" in record["exception"]
    assert "PRIVATE_GROUP" not in stream.getvalue()
    assert "PRIVATE_CHILD" not in stream.getvalue()
