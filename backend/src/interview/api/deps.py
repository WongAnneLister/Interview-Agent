"""路由依赖。

所有业务路由都必须通过 get_current_user_id() 拿当前用户，并用它绑定会话
（db/scope.py 的 user_id 拦截）。
v0 只有一个本地用户，直接返回配置里的 DEFAULT_USER_ID；以后接登录只改这个函数。
routes_without_user() 列出漏掉这个依赖的路由，单元测试会检查它为空。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from interview.api.errors import ServiceUnavailableError
from interview.api.resources import Resources
from interview.config import Settings
from interview.db.models import UsageRole
from interview.providers.base import LLMProvider
from interview.providers.observed import CallContext
from interview.providers.registry import ProviderNotConfigured, ProviderRegistry
from interview.storage.base import ObjectStore

# 不需要用户的路由（健康检查、指标）
PUBLIC_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})


def get_resources(request: Request) -> Resources:
    resources: Resources = request.app.state.resources
    return resources


def get_settings(resources: Annotated[Resources, Depends(get_resources)]) -> Settings:
    return resources.settings


def get_current_user_id(settings: Annotated[Settings, Depends(get_settings)]) -> uuid.UUID:
    """当前请求的用户。v0：配置里的 DEFAULT_USER_ID。"""
    user_id = settings.app.default_user_id
    if user_id is None:
        raise ServiceUnavailableError(
            "服务还没有初始化本地用户，请先运行 make seed", code="user_not_configured"
        )
    return user_id


async def get_session(
    resources: Annotated[Resources, Depends(get_resources)],
) -> AsyncIterator[AsyncSession]:
    """每个请求一个会话，由调用方显式 commit；请求结束时关闭（未提交的事务回滚）。"""
    async with resources.sessionmaker() as session:
        yield session


def get_providers(resources: Annotated[Resources, Depends(get_resources)]) -> ProviderRegistry:
    return resources.providers


CurrentUserId = Annotated[uuid.UUID, Depends(get_current_user_id)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
Providers = Annotated[ProviderRegistry, Depends(get_providers)]


def get_storage(resources: Annotated[Resources, Depends(get_resources)]) -> ObjectStore:
    if resources.storage is None:
        raise ServiceUnavailableError("对象存储未配置")
    return resources.storage


def get_resume_llm(user_id: CurrentUserId, providers: Providers) -> LLMProvider:
    """简历解析走评估模型，用量记在 resume_parse。"""
    try:
        return providers.llm(
            "llm_eval", CallContext(user_id=user_id, usage_role=UsageRole.RESUME_PARSE)
        )
    except ProviderNotConfigured as exc:
        raise ServiceUnavailableError("评估模型未配置，无法解析简历") from exc


def _uses(dependant: Dependant, target: object) -> bool:
    return dependant.call is target or any(_uses(d, target) for d in dependant.dependencies)


def routes_without_user(app: FastAPI) -> list[str]:
    """没有（直接或间接）依赖 get_current_user_id 的非公开路由。"""
    missing: list[str] = []
    for route in app.routes:
        if (
            isinstance(route, APIRoute)
            and route.path not in PUBLIC_PATHS
            and not _uses(route.dependant, get_current_user_id)
        ):
            missing.append(f"{sorted(route.methods or [])} {route.path}")
    return missing
