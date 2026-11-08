"""开跑前探测：任一模型角色失败就报告，且探测时不走备用模型。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from evals import preflight
from interview.config import ProvidersSettings
from interview.observability import usage
from interview.providers.base import ProviderUnavailable
from interview.providers.fake import FakeLLM
from interview.providers.registry import ProviderFactories, ProviderRegistry


def settings(**roles: Any) -> ProvidersSettings:
    base = {
        "llm_front": {"provider": "fake", "model": "front"},
        "llm_eval": {"provider": "fake", "model": "eval"},
        "llm_live_eval": {"provider": "fake", "model": "live"},
        "llm_report": {"provider": "", "model": ""},
        "llm_fallback": {"provider": "fake", "model": "backup"},
        "stt": {"provider": "fake", "model": "stt"},
        "tts": {"provider": "fake", "model": "tts"},
    }
    return ProvidersSettings.model_validate(base | roles)


async def test_probe_reports_failing_roles_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[Any] = []

    async def ignore_usage(**_kwargs: Any) -> None:
        return None

    # 本测试只检查探测与主备行为；用量落库会间接读取本机 .env（C12）。
    monkeypatch.setattr(usage, "record_usage", ignore_usage)

    def factories(s: ProvidersSettings) -> ProviderFactories:
        def llm(role: Any) -> FakeLLM:
            created.append(role.model)
            errors = (
                [ProviderUnavailable("连接失败", provider="fake", model=role.model)]
                if role.model == "eval"
                else []
            )
            return FakeLLM("ok", model=role.model, errors=errors)

        return ProviderFactories(llm={"fake": llm})

    original = ProviderRegistry.__init__

    def init(self: ProviderRegistry, s: ProvidersSettings, **kwargs: Any) -> None:
        original(self, s, factories=factories(s), **kwargs)

    monkeypatch.setattr(ProviderRegistry, "__init__", init)
    problems = await preflight.probe(settings(), uuid.uuid4())
    assert len(problems) == 1 and problems[0].startswith(
        "llm_eval（fake/eval）：ProviderUnavailable"
    )
    assert "backup" not in created
