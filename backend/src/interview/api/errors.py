"""统一错误格式与业务异常。

所有错误响应都是 {"error": {"code", "message", "trace_id"}}：
- AppError 及子类：业务异常，code / message / 状态码由异常给出，message 是可以展示给用户的中文说明。
- 框架的 HTTPException（如路由不存在 404、方法不允许 405）：code 取状态码对应的短名。
- 请求参数校验失败：422 validation_error，message 只列出字段位置和错误类型，不回显输入值。
- 未捕获的异常：500 internal_error，只返回固定说明和 trace_id，不泄露异常信息和堆栈；
  完整堆栈由请求中间件写入日志。
trace_id 与日志、Langfuse 里的 trace_id 相同，用户反馈问题时提供它即可定位。
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from interview.api.middleware import current_trace_id


class AppError(Exception):
    """业务异常基类。子类设置 status_code 和 code，抛出时给出面向用户的 message。"""

    status_code: int = HTTPStatus.BAD_REQUEST
    code: str = "bad_request"

    def __init__(
        self, message: str, *, code: str | None = None, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class NotFoundError(AppError):
    status_code = HTTPStatus.NOT_FOUND
    code = "not_found"


class ConflictError(AppError):
    status_code = HTTPStatus.CONFLICT
    code = "conflict"


class ServiceUnavailableError(AppError):
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    code = "service_unavailable"


def error_response(
    status_code: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "trace_id": current_trace_id()}}
    return JSONResponse(body, status_code=status_code, headers=headers)


_FRAMEWORK_MESSAGES = {
    404: "接口不存在",
    405: "不支持这个请求方法",
    413: "请求内容太大",
    415: "不支持的内容类型",
}


def _status_code_name(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase.lower().replace(" ", "_").replace("-", "_")
    except ValueError:
        return f"http_{status_code}"


async def _app_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)  # noqa: S101 — 由注册方式保证
    return error_response(exc.status_code, exc.code, exc.message)


async def _http_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    message = exc.detail or _status_code_name(exc.status_code)
    if message == HTTPStatus(exc.status_code).phrase:  # 框架默认的英文说明换成中文
        message = _FRAMEWORK_MESSAGES.get(exc.status_code, message)
    headers = dict(exc.headers) if exc.headers else None
    return error_response(exc.status_code, _status_code_name(exc.status_code), message, headers)


async def _validation_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    problems: list[str] = []
    for error in exc.errors()[:10]:
        detail: dict[str, Any] = dict(error)
        location = ".".join(str(part) for part in detail.get("loc", ())) or "(请求)"
        problems.append(f"{location}：{detail.get('msg', '')}")
    return error_response(422, "validation_error", "请求参数不合法：" + "；".join(problems))


async def _unhandled_error(_request: Request, _exc: Exception) -> JSONResponse:
    return error_response(500, "internal_error", "服务器内部错误，请稍后重试")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    # Exception 的处理器由 Starlette 的 ServerErrorMiddleware 调用（500）
    app.add_exception_handler(Exception, _unhandled_error)
