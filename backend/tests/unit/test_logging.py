"""结构化日志：JSON 输出、trace_id / interview_id 上下文、脱敏。"""

import asyncio
import io
import json
from typing import Any

import structlog

from interview.logging import configure_logging, log_context, new_trace_id, redact


def capture() -> io.StringIO:
    stream = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=stream)
    return stream


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_output_has_trace_and_interview_id() -> None:
    stream = capture()
    with log_context(interview_id="iv-1") as trace_id:
        structlog.get_logger().info("turn_started", seq=3)
    structlog.get_logger().info("outside")

    inside, outside = lines(stream)
    assert inside["event"] == "turn_started"
    assert inside["trace_id"] == trace_id
    assert inside["interview_id"] == "iv-1"
    assert inside["level"] == "info"
    assert "timestamp" in inside
    assert "trace_id" not in outside


def test_nested_context_inherits_trace_id() -> None:
    capture()
    with log_context(trace_id="outer") as outer, log_context(interview_id="iv-2") as inner:
        assert inner == outer == "outer"


def test_context_is_isolated_between_tasks() -> None:
    stream = capture()

    async def worker(interview_id: str) -> None:
        with log_context(interview_id=interview_id):
            await asyncio.sleep(0)
            structlog.get_logger().info("tick")

    async def main() -> None:
        await asyncio.gather(worker("a"), worker("b"))

    asyncio.run(main())
    records = lines(stream)
    assert {r["interview_id"] for r in records} == {"a", "b"}
    assert records[0]["trace_id"] != records[1]["trace_id"]


def test_new_trace_id_format() -> None:
    tid = new_trace_id()
    assert len(tid) == 32
    int(tid, 16)
    assert tid != new_trace_id()


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
    with log_context(trace_id="abc"):
        structlog.get_logger().info("hello", api_key="sk-789")
    text = stream.getvalue()
    assert "hello" in text
    assert "abc" in text
    assert "sk-789" not in text


def test_level_filtering() -> None:
    stream = io.StringIO()
    configure_logging(level="WARNING", stream=stream)
    structlog.get_logger().info("dropped")
    structlog.get_logger().warning("kept")
    assert [r["event"] for r in lines(stream)] == ["kept"]
