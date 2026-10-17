"""请求 ID 与访问日志。

请求 ID 就是这个请求的 trace_id（32 位小写十六进制，W3C trace-id 格式）：
- 请求头带合法的 X-Request-ID（32 位十六进制、非全 0）时沿用，否则新生成；不合法的原值截断后
  记为 client_request_id 方便对照。
- 它同时用作 OpenTelemetry 根 span 的 trace_id（tracing.use_trace_id），所以日志、Langfuse、
  错误响应里的 trace_id 是同一个；响应头 X-Request-ID 返回它。
  例外：请求自带 W3C traceparent 时，链路沿用调用方的 trace_id，日志里的 trace_id 以链路为准。
- 访问日志 http_request：方法、路径、状态码、耗时；健康检查和指标抓取不记录（太频繁）。
- 处理过程中未捕获的异常：错误处理器已返回 500，这里把完整堆栈写入日志，不再向外抛出。

实现为 FastAPI 子类的 __call__，位于所有中间件（包括 OpenTelemetry 自动埋点）之外，
这样根 span 创建前 trace_id 就已确定。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from contextvars import ContextVar
from typing import Any

import structlog
from fastapi import FastAPI
from starlette.types import Message, Receive, Scope, Send
from structlog.contextvars import bound_contextvars

from interview.observability.tracing import use_trace_id

REQUEST_ID_HEADER = "x-request-id"
QUIET_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})
_VALID_ID = re.compile(r"^[0-9a-f]{32}$")

_current_trace_id: ContextVar[str | None] = ContextVar("api_request_trace_id", default=None)

log = structlog.get_logger("interview.api")


def current_trace_id() -> str | None:
    """当前请求的 trace_id；不在请求内时为空。"""
    return _current_trace_id.get()


def resolve_request_id(raw: str | None) -> tuple[str, str | None]:
    """返回（使用的请求 ID, 被替换掉的原始值）。"""
    if raw is not None:
        candidate = raw.strip().lower()
        if _VALID_ID.match(candidate) and int(candidate, 16) != 0:
            return candidate, None
        printable = "".join(ch for ch in raw if ch.isprintable())[:64]
        return uuid.uuid4().hex, printable or None
    return uuid.uuid4().hex, None


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


async def _send_internal_error(send: Send, request_id: str) -> None:
    body = json.dumps(
        {
            "error": {
                "code": "internal_error",
                "message": "服务器内部错误，请稍后重试",
                "trace_id": request_id,
            }
        },
        ensure_ascii=False,
    ).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"x-request-id", request_id.encode()),
    ]
    await send({"type": "http.response.start", "status": 500, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class InterviewAPI(FastAPI):
    """FastAPI 子类：在整个中间件栈之外处理请求 ID、访问日志和兜底异常。"""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await super().__call__(scope, receive, send)
            return
        request_id, client_value = resolve_request_id(_header(scope, b"x-request-id"))
        status = 500
        response_started = False
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                headers: list[Any] = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        token = _current_trace_id.set(request_id)
        fields: dict[str, Any] = {"trace_id": request_id}
        if client_value:
            fields["client_request_id"] = client_value
        try:
            with bound_contextvars(**fields), use_trace_id(int(request_id, 16)):
                try:
                    await super().__call__(scope, receive, send_with_id)
                except Exception:
                    # 错误处理器已经返回了 500；这里只记录堆栈，不再抛给服务器
                    log.exception(
                        "http_unhandled_error", method=scope["method"], path=scope["path"]
                    )
                    status = 500
                    if not response_started:  # 正常情况下错误处理器已经返回；兜底给出同样格式的 500
                        await _send_internal_error(send, request_id)
                finally:
                    path: str = scope["path"]
                    if path not in QUIET_PATHS:
                        log.log(
                            40 if status >= 500 else 20,
                            "http_request",
                            method=scope["method"],
                            path=path,
                            status=status,
                            duration_ms=round((time.perf_counter() - started) * 1000, 1),
                        )
        finally:
            _current_trace_id.reset(token)
