"""可观测性冒烟：产生一条完整链路发到 Langfuse，并在评测进程的指标端口上暴露示例指标。

用法：make obs-smoke（需要 .env 中 OBSERVABILITY_ENABLED=true、Langfuse 配置，以及 make tunnel）。
链路：obs_smoke（根）→ httpx 客户端 → FastAPI 路由 → SQLAlchemy 查询 + 假的 generation
      → httpx 请求 Langfuse 健康检查。
指标：interview_smoke_total、interview_smoke_seconds，保持 --hold 秒供 Prometheus 抓取。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid

import httpx
import sqlalchemy
import structlog
from fastapi import FastAPI
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.trace import format_trace_id
from pydantic_settings import BaseSettings

from interview.config import (
    DEFAULT_ENV_FILE,
    AppSettings,
    LangfuseSettings,
    MetricsSettings,
    ObservabilitySettings,
)
from interview.logging import configure_logging
from interview.observability.instrument import (
    instrument_fastapi,
    instrument_httpx,
    instrument_sqlalchemy,
)
from interview.observability.prometheus import counter, histogram, start_metrics_server
from interview.observability.tracing import (
    init_tracing,
    llm_span,
    set_llm_output,
    shutdown_tracing,
    span,
    tracer_provider,
)

LANGFUSE_PROJECT_ID = "interview-agent-dev"  # 与 deploy/vps/docker-compose.yml 的无界面初始化一致

log = structlog.get_logger()


def read[T: BaseSettings](cls: type[T]) -> T:
    """只读取本脚本用到的配置组，不要求数据库、LiveKit 等配置齐全。"""
    return cls(_env_file=DEFAULT_ENV_FILE)


def build_app(engine: sqlalchemy.Engine) -> FastAPI:
    app = FastAPI()

    @app.post("/smoke/turn")
    async def turn() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        with engine.connect() as conn:
            conn.execute(sqlalchemy.text("SELECT 1"))
        with llm_span(
            "llm.front",
            provider="fake",
            model="fake-model",
            role="front",
            input=[{"role": "user", "content": "冒烟测试"}],
            model_parameters={"temperature": 0.3},
        ) as generation:
            await asyncio.sleep(0.05)
            set_llm_output(generation, "收到", usage={"input": 12, "output": 8, "total": 20})
        log.info("smoke_turn_done")
        return {"ok": "1"}

    return app


async def run(hold_s: int) -> int:
    app_settings = read(AppSettings)
    observability = read(ObservabilitySettings)
    langfuse = read(LangfuseSettings)
    metrics = read(MetricsSettings)
    configure_logging(level=app_settings.log_level, fmt=app_settings.log_format)
    if not (observability.enabled and langfuse.configured):
        print("需要 OBSERVABILITY_ENABLED=true 和 LANGFUSE_*（make vps-env 写入）", file=sys.stderr)
        return 1

    init_tracing("eval", observability, langfuse, environment=app_settings.app_env)
    instrument_httpx(enabled=True)
    engine = sqlalchemy.create_engine("sqlite://")
    instrument_sqlalchemy(engine, enabled=True)
    app = build_app(engine)
    instrument_fastapi(app, enabled=True)
    start_metrics_server(metrics.eval_metrics_port, enabled=True)
    calls = counter("smoke", "冒烟脚本执行次数", ["result"])
    seconds = histogram("smoke_seconds", "冒烟链路耗时（秒）")

    interview_id = f"smoke-{uuid.uuid4().hex[:8]}"
    started = time.perf_counter()
    with span("obs_smoke", interview_id=interview_id, role="eval", is_eval=True) as root:
        log.info("smoke_started", interview_id=interview_id)
        local = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://smoke", timeout=10
        )
        HTTPXClientInstrumentor.instrument_client(local, tracer_provider=tracer_provider())
        async with local:
            (await local.post("/smoke/turn")).raise_for_status()
        async with httpx.AsyncClient(timeout=10) as client:
            health = await client.get(f"{langfuse.host.rstrip('/')}/api/public/health")
        result = "ok" if health.is_success else "langfuse_unhealthy"
    seconds.observe(time.perf_counter() - started)
    calls.labels(result=result).inc()
    trace_id = format_trace_id(root.get_span_context().trace_id)
    shutdown_tracing()  # 导出剩余 span

    print(f"链路已发送（{result}）：interview_id={interview_id} trace_id={trace_id}")
    print(f"Langfuse：{langfuse.host.rstrip('/')}/project/{LANGFUSE_PROJECT_ID}/traces/{trace_id}")
    print(f"指标：http://127.0.0.1:{metrics.eval_metrics_port}/metrics，保持 {hold_s} 秒供抓取")
    await asyncio.sleep(hold_s)
    return 0 if result == "ok" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hold", type=int, default=60, help="发送后保持指标端口的秒数")
    return asyncio.run(run(parser.parse_args().hold))


if __name__ == "__main__":
    sys.exit(main())
