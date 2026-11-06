"""开跑前探测：每个已配置的大模型角色关掉主备切换各发一次极短请求。

主模型连不上时评测会悄悄跑在备用模型上，分数就不再代表配置的模型（见 challenges C18），
所以有任何角色失败就直接退出。
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview.config import LLMFallbackSettings, ProvidersSettings
from interview.db.models import UsageRole
from interview.providers.observed import CallContext, eval_mode
from interview.providers.registry import LLMSlot, ProviderRegistry

PROBE_TIMEOUT_S = 20.0


async def probe(
    settings: ProvidersSettings,
    user_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> list[str]:
    """返回失败说明；全部可用时为空列表。探测用量记在评测用户名下，计入本次费用。"""
    primary_only = settings.model_copy(
        update={"llm_fallback": LLMFallbackSettings.model_construct()}
    )
    registry = ProviderRegistry(primary_only, sessionmaker=sessionmaker)
    slots: list[LLMSlot] = ["llm_front", "llm_eval"]
    if settings.llm_live_eval.configured:
        slots.append("llm_live_eval")

    async def one(slot: LLMSlot) -> str | None:
        role = getattr(settings, slot)
        llm = registry.llm(slot, CallContext(user_id=user_id, usage_role=UsageRole.EVAL_HARNESS))
        try:
            await llm.complete(
                [{"role": "user", "content": "回复 ok"}],
                max_tokens=8,
                temperature=0,
                timeout_s=PROBE_TIMEOUT_S,
            )
        except Exception as exc:
            return f"{slot}（{role.provider}/{role.model}）：{type(exc).__name__}: {exc}"
        return None

    try:
        with eval_mode():
            results = await asyncio.gather(*(one(slot) for slot in slots))
    finally:
        await registry.aclose()
    return [item for item in results if item is not None]
