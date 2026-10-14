"""Prometheus 指标：计数器 / 直方图 / 仪表的注册工具，以及 API、worker、评测进程的暴露方式。

- 指标名统一加 interview_ 前缀；同名重复注册返回已有对象（模块被多处导入时不会报错）。
- 使用默认 REGISTRY，自带进程 CPU、内存、GC 等指标。
- 可观测性关闭时不暴露（不挂 /metrics、不监听端口）；计数器仍是合法对象，只在内存里累加。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from threading import Lock
from wsgiref.simple_server import WSGIServer

from fastapi import FastAPI, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)

PREFIX = "interview_"

_metrics: dict[str, tuple[Counter | Histogram | Gauge, tuple[str, ...]]] = {}
_lock = Lock()


def _get_or_create[M: (Counter, Histogram, Gauge)](
    kind: type[M], name: str, labelnames: Sequence[str], create: Callable[[str, tuple[str, ...]], M]
) -> M:
    full = PREFIX + name
    labels = tuple(labelnames)
    with _lock:
        existing = _metrics.get(full)
        if existing is None:
            metric = create(full, labels)
            _metrics[full] = (metric, labels)
            return metric
    metric, known_labels = existing
    if not isinstance(metric, kind) or known_labels != labels:
        raise ValueError(f"指标 {full} 已用不同的类型或标签注册过")
    return metric


def counter(name: str, documentation: str, labelnames: Sequence[str] = ()) -> Counter:
    """注册（或取回）计数器。name 不含前缀；按 Prometheus 惯例导出时会自动加 _total。"""
    return _get_or_create(
        Counter, name, labelnames, lambda full, labels: Counter(full, documentation, labels)
    )


def histogram(
    name: str,
    documentation: str,
    labelnames: Sequence[str] = (),
    buckets: Sequence[float] = Histogram.DEFAULT_BUCKETS,
) -> Histogram:
    """注册（或取回）直方图；耗时类指标用秒作单位，buckets 默认用 prometheus_client 的默认值。"""
    return _get_or_create(
        Histogram,
        name,
        labelnames,
        lambda full, labels: Histogram(full, documentation, labels, buckets=tuple(buckets)),
    )


def gauge(name: str, documentation: str, labelnames: Sequence[str] = ()) -> Gauge:
    """注册（或取回）仪表：表示当前值（如当日累计成本），可增可减。"""
    return _get_or_create(
        Gauge, name, labelnames, lambda full, labels: Gauge(full, documentation, labels)
    )


def _metrics_endpoint() -> Response:
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


def mount_metrics(app: FastAPI, *, enabled: bool) -> None:
    """API 进程：在应用上挂 GET /metrics（不进 OpenAPI 文档）。"""
    if enabled:
        app.add_api_route("/metrics", _metrics_endpoint, methods=["GET"], include_in_schema=False)


def start_metrics_server(port: int, *, enabled: bool) -> WSGIServer | None:
    """worker / 评测进程：在 127.0.0.1:port 上用后台线程暴露 /metrics；关闭时返回 None。"""
    if not enabled:
        return None
    server, _thread = start_http_server(port, addr="127.0.0.1")
    return server
