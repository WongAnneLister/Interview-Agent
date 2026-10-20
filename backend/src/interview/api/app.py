"""API 应用工厂。

运行：make api（uvicorn interview.api.app:create_app --factory --reload）。

- create_app() 读取配置、初始化日志与链路追踪、安装中间件和错误处理器；
  lifespan 启动时创建数据库、Redis、厂商注册表并校验全部提示词模板，关闭时释放。
- /healthz：进程存活；/readyz：检查数据库与 Redis，任一失败返回 503。两者都不需要用户。
- CORS 只允许配置 WEB_ORIGINS 里的前端地址；为空时不允许任何跨域请求。
- 业务路由都通过 deps.get_current_user_id 拿用户。
- 文字面试 WebSocket 见 routes/text_ws.py，它的消息模型并入 OpenAPI 的 components。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Annotated

import structlog
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from interview.api.deps import get_resources
from interview.api.errors import install_error_handlers
from interview.api.middleware import InterviewAPI
from interview.api.resources import Resources, create_resources
from interview.api.routes.interviews import router as interviews_router
from interview.api.routes.jd import router as jd_router
from interview.api.routes.resumes import router as resumes_router
from interview.api.routes.text_ws import openapi_schemas
from interview.api.routes.text_ws import router as text_ws_router
from interview.api.upload_limit import ResumeUploadGuard
from interview.config import Settings, get_settings
from interview.logging import configure_logging
from interview.observability.instrument import instrument_fastapi, instrument_httpx
from interview.observability.prometheus import mount_metrics
from interview.observability.tracing import init_tracing, shutdown_tracing
from interview.prompts.loader import get_prompt_library

log = structlog.get_logger("interview.api")

type ResourceFactory = Callable[[Settings], Resources]


def create_app(
    settings: Settings | None = None,
    *,
    resource_factory: ResourceFactory = create_resources,
    init_observability: bool = True,
) -> FastAPI:
    """init_observability=False 时不配置日志和链路追踪（测试用，避免改动进程全局状态）。"""
    settings = settings or get_settings()
    observability = settings.observability.enabled
    if init_observability:
        configure_logging(level=settings.app.log_level, fmt=settings.app.log_format)
        init_tracing(
            "api", settings.observability, settings.langfuse, environment=settings.app.app_env
        )
        instrument_httpx(enabled=observability)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        get_prompt_library()  # 启动时校验全部提示词模板，出错直接启动失败
        resources = resource_factory(settings)
        app.state.resources = resources
        if settings.app.default_user_id is None:
            log.warning("default_user_missing", hint="运行 make seed 创建本地用户")
        log.info("api_started", env=settings.app.app_env)
        try:
            yield
        finally:
            await resources.close()
            if init_observability:
                shutdown_tracing()
            log.info("api_stopped")

    app = InterviewAPI(title="Interview Agent API", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    # CORS 包住整个 HTTP 处理（包括 ServerErrorMiddleware 与兜底的 500）。
    app.configure_cors(settings.app.web_origins)
    app.add_middleware(ResumeUploadGuard)
    app.add_api_route("/healthz", healthz, methods=["GET"], include_in_schema=False)
    app.add_api_route("/readyz", readyz, methods=["GET"], include_in_schema=False)
    app.include_router(resumes_router)
    app.include_router(jd_router)
    app.include_router(interviews_router)
    app.include_router(text_ws_router)
    app.extra_schemas = openapi_schemas()
    mount_metrics(app, enabled=observability)
    instrument_fastapi(app, enabled=observability)
    return app


async def healthz() -> dict[str, str]:
    """进程存活即返回 ok，不检查依赖。"""
    return {"status": "ok"}


async def readyz(resources: Annotated[Resources, Depends(get_resources)]) -> JSONResponse:
    """数据库与 Redis 都可用时 200，否则 503；只返回检查结果，不含连接信息。"""
    checks = await resources.readiness()
    ready = all(result == "ok" for result in checks.values())
    body = {"status": "ready" if ready else "not_ready", "checks": checks}
    return JSONResponse(body, status_code=200 if ready else 503)
