"""Prometheus 工具：注册去重、/metrics 暴露、独立端口、关闭时不暴露。"""

import socket
import urllib.request

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from interview.observability.prometheus import (
    counter,
    histogram,
    mount_metrics,
    start_metrics_server,
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_register_is_idempotent() -> None:
    a = counter("test_idem_calls", "调用次数", ["role"])
    assert counter("test_idem_calls", "调用次数", ["role"]) is a
    h = histogram("test_idem_seconds", "耗时", buckets=[0.1, 1])
    assert histogram("test_idem_seconds", "耗时", buckets=[0.1, 1]) is h


def test_register_conflict_raises() -> None:
    counter("test_conflict", "x", ["role"])
    with pytest.raises(ValueError, match="interview_test_conflict"):
        counter("test_conflict", "x", ["provider"])
    with pytest.raises(ValueError):
        histogram("test_conflict", "x", ["role"])


def test_api_metrics_endpoint() -> None:
    counter("test_api_requests", "请求数", ["role"]).labels(role="front").inc(2)
    histogram("test_api_seconds", "耗时").observe(0.2)
    app = FastAPI()
    mount_metrics(app, enabled=True)
    resp = TestClient(app).get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert 'interview_test_api_requests_total{role="front"} 2.0' in resp.text
    assert "interview_test_api_seconds_count 1.0" in resp.text
    assert "/metrics" not in app.openapi()["paths"]


def test_disabled_does_not_expose() -> None:
    app = FastAPI()
    mount_metrics(app, enabled=False)
    assert TestClient(app).get("/metrics").status_code == 404
    assert start_metrics_server(free_port(), enabled=False) is None
    counter("test_disabled_calls", "仍可正常使用").inc()


def test_worker_metrics_server_on_localhost() -> None:
    counter("test_worker_ticks", "worker 心跳").inc()
    port = free_port()
    server = start_metrics_server(port, enabled=True)
    assert server is not None
    try:
        assert server.server_address[0] == "127.0.0.1"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
            assert b"interview_test_worker_ticks_total 1.0" in resp.read()
    finally:
        server.shutdown()
        server.server_close()
