"""第三方库自动埋点：FastAPI（服务端）、httpx（客户端）、SQLAlchemy（数据库）。

在 init_tracing() 之后调用，span 使用 tracing 模块的 TracerProvider；可观测性关闭时都不做任何事。
"""

from __future__ import annotations

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import (  # pyright: ignore[reportMissingTypeStubs]
    SQLAlchemyInstrumentor,
)
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

from interview.observability.tracing import tracer_provider

# 不产生 span 的路径（逗号分隔的正则）：指标抓取和健康检查太频繁，没有排查价值
EXCLUDED_URLS = "/metrics,/healthz,/readyz"


def instrument_fastapi(app: FastAPI, *, enabled: bool) -> None:
    """每个 FastAPI 应用调用一次（在应用工厂里）。"""
    if enabled:
        FastAPIInstrumentor.instrument_app(
            app, tracer_provider=tracer_provider(), excluded_urls=EXCLUDED_URLS
        )


def instrument_httpx(*, enabled: bool) -> None:
    """进程内所有 httpx.Client / AsyncClient 的请求都会产生客户端 span。"""
    if enabled:
        HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider())


def instrument_sqlalchemy(engine: Engine | AsyncEngine, *, enabled: bool) -> None:
    """给业务库引擎加埋点；异步引擎埋在其底层同步引擎上。

    SQLAlchemyInstrumentor 是进程内单例，重复调用会被忽略，v0 每个进程只有一个业务库引擎。
    """
    if enabled:
        sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
        SQLAlchemyInstrumentor().instrument(engine=sync_engine, tracer_provider=tracer_provider())


def uninstrument_all() -> None:
    """撤销 httpx 和 SQLAlchemy 的全局埋点（测试用）。"""
    HTTPXClientInstrumentor().uninstrument()
    SQLAlchemyInstrumentor().uninstrument()
