"""离线导出前端使用的 OpenAPI：不读取配置、不启动 lifespan。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import NoReturn

from pydantic import SecretStr

from interview.api.app import create_app
from interview.config import (
    AppSettings,
    CostSettings,
    DatabaseSettings,
    LangfuseSettings,
    LiveKitSettings,
    LLMEvalSettings,
    LLMFallbackSettings,
    LLMFrontSettings,
    MetricsSettings,
    ObservabilitySettings,
    ProvidersSettings,
    RedisSettings,
    ReportSettings,
    S3Settings,
    Settings,
    STTSettings,
    TTSSettings,
)


def _no_resources(settings: Settings) -> NoReturn:
    raise RuntimeError("OpenAPI 导出不能启动 lifespan 或创建外部资源")


def export_schema() -> str:
    """返回确定的 JSON 文本，供命令行与快照测试共用。"""
    # BaseSettings 的构造器即使传了字段也会解析环境变量；model_construct
    # 只使用下面的占位值与字段默认值。report 也显式构造，避开它的配置工厂。
    settings = Settings(
        app=AppSettings.model_construct(app_env="test"),
        db=DatabaseSettings.model_construct(url=SecretStr("postgresql+asyncpg://unused")),
        redis=RedisSettings.model_construct(url=SecretStr("redis://unused")),
        livekit=LiveKitSettings.model_construct(
            url="ws://unused", api_key=SecretStr("unused"), api_secret=SecretStr("unused")
        ),
        s3=S3Settings.model_construct(
            endpoint="http://unused", access_key=SecretStr("unused"), secret_key=SecretStr("unused")
        ),
        providers=ProvidersSettings(
            llm_front=LLMFrontSettings.model_construct(),
            llm_eval=LLMEvalSettings.model_construct(),
            llm_fallback=LLMFallbackSettings.model_construct(),
            stt=STTSettings.model_construct(),
            tts=TTSSettings.model_construct(),
        ),
        observability=ObservabilitySettings.model_construct(enabled=False),
        langfuse=LangfuseSettings.model_construct(),
        metrics=MetricsSettings.model_construct(),
        cost=CostSettings.model_construct(),
        report=ReportSettings.model_construct(),
    )
    app = create_app(settings, resource_factory=_no_resources, init_observability=False)
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="OpenAPI JSON 输出路径")
    args = parser.parse_args()
    args.output.write_text(export_schema(), encoding="utf-8")


if __name__ == "__main__":
    main()
