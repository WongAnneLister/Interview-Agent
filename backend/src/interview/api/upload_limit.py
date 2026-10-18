"""简历上传的请求体上限。

Starlette 会把文件 part 整段写入临时文件，路由里再检查 10 MB 已经晚了。
这里在交给解析器之前卡住原始请求体：Content-Length 已经超限时不读正文；
分块上传读到上限就停，返回 413。余量只留给 multipart 的边界和头部。
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from interview.api.middleware import current_trace_id
from interview.domain.resume import MAX_RESUME_UPLOAD_BODY

_PATHS = frozenset({"/api/resumes", "/api/resumes/"})


def _is_upload(scope: Scope) -> bool:
    return (
        scope.get("type") == "http"
        and scope.get("method") == "POST"
        and scope.get("path") in _PATHS
    )


def _content_length(scope: Scope) -> int | None:
    for key, value in scope.get("headers", []):
        if key.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _send_too_large(send: Send) -> None:
    body = json.dumps(
        {
            "error": {
                "code": "file_too_large",
                "message": "简历不能超过 10 MB",
                "trace_id": current_trace_id(),
            }
        },
        ensure_ascii=False,
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _replay(body: bytes) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


async def _read_body(scope: Scope, receive: Receive) -> bytes | None:
    """读完并返回请求体。超过上限时返回 None；声明的长度已经超限时一次都不读。"""
    declared = _content_length(scope)
    if declared is not None and declared > MAX_RESUME_UPLOAD_BODY:
        return None
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        kind = message["type"]
        if kind == "http.disconnect":
            return b"".join(chunks)
        if kind != "http.request":
            continue
        chunk = bytes(message.get("body", b""))
        total += len(chunk)
        if total > MAX_RESUME_UPLOAD_BODY:
            return None
        if chunk:
            chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


class ResumeUploadGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _is_upload(scope):
            await self.app(scope, receive, send)
            return
        body = await _read_body(scope, receive)
        if body is None:
            await _send_too_large(send)
            return
        await self.app(scope, _replay(body), send)
