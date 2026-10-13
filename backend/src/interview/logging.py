"""structlog 结构化日志：默认输出 JSON，开发时可切换为彩色可读格式。

trace_id、interview_id 通过 contextvars 绑定，在同一请求 / 同一 asyncio 任务链里
打出的每条日志都会自动带上。所有日志在输出前都经过 redact() 脱敏。
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Generator, MutableMapping
from contextlib import contextmanager
from typing import Any, Literal, TextIO

import structlog
from structlog.contextvars import bound_contextvars, get_contextvars, merge_contextvars
from structlog.typing import EventDict, Processor, WrappedLogger

REDACTED = "***"

# 字段名包含这些词时值替换为 ***。token 不匹配 tokens，避免把 input_tokens 等用量计数也遮掉。
_SENSITIVE_KEY = re.compile(r"api[-_]?key|secret|password|token(?!s)", re.IGNORECASE)
# 这些字段只记录长度，不输出内容
_LENGTH_ONLY_KEYS = frozenset({"resume_text", "transcript"})


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _redact_mapping(value)  # pyright: ignore[reportUnknownArgumentType]
    if isinstance(value, list | tuple):
        return [_redact_value(item) for item in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def _redact_mapping(data: MutableMapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in _LENGTH_ONLY_KEYS:
            out[f"{key}_len"] = len(value) if hasattr(value, "__len__") else None
        elif _SENSITIVE_KEY.search(key):
            out[key] = REDACTED
        else:
            out[key] = _redact_value(value)
    return out


def redact(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """structlog 处理器：遮盖密钥类字段，正文类字段只保留长度（递归处理嵌套 dict / list）。"""
    return _redact_mapping(event_dict)


def configure_logging(
    *,
    level: str = "INFO",
    fmt: Literal["json", "console"] = "json",
    stream: TextIO | None = None,
) -> None:
    """进程启动时调用一次。stream 默认 stdout，测试中可传入 StringIO。"""
    processors: list[Processor] = [
        merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact,
        structlog.processors.StackInfoRenderer(),
    ]
    if fmt == "console":
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        processors += [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(stream),
        cache_logger_on_first_use=False,
    )


def new_trace_id() -> str:
    """32 位十六进制，与 OpenTelemetry trace_id 格式一致（PR-04b 改为取当前 span 的值）。"""
    return uuid.uuid4().hex


@contextmanager
def log_context(
    *, trace_id: str | None = None, interview_id: str | None = None, **extra: Any
) -> Generator[str]:
    """在 with 块内绑定日志上下文，退出时恢复原值；返回生效的 trace_id。

    未传 trace_id 时沿用外层已绑定的值，外层也没有则新生成一个。
    """
    current = get_contextvars().get("trace_id")
    tid = trace_id or (current if isinstance(current, str) else None) or new_trace_id()
    fields: dict[str, Any] = {"trace_id": tid, **extra}
    if interview_id is not None:
        fields["interview_id"] = interview_id
    with bound_contextvars(**fields):
        yield tid
