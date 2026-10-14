"""统一读取配置：环境变量优先，其次是仓库根目录的 .env。

按模块分组，每组对应一个环境变量前缀（如 DATABASE_、S3_、LLM_FRONT_）。
必填项缺失或为空时，get_settings() 抛出 ConfigError 并列出所有缺失的环境变量名，
进程在启动阶段就会失败，而不是等到第一次用到时才出错。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/interview/config.py → 仓库根目录
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"


class ConfigError(RuntimeError):
    """必填配置缺失或格式不正确。"""


def _config(prefix: str) -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=prefix,
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # .env 里留空的项视为未配置
        extra="ignore",  # .env 里还有 VPS_* 等不属于本组的变量
    )


class AppSettings(BaseSettings):
    model_config = _config("")

    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"


class DatabaseSettings(BaseSettings):
    model_config = _config("DATABASE_")

    url: SecretStr  # 连接串里含密码


class RedisSettings(BaseSettings):
    model_config = _config("REDIS_")

    url: SecretStr


class LiveKitSettings(BaseSettings):
    model_config = _config("LIVEKIT_")

    url: str
    api_key: SecretStr
    api_secret: SecretStr


class S3Settings(BaseSettings):
    """S3 兼容对象存储（开发用 SeaweedFS，生产可换 OSS）。"""

    model_config = _config("S3_")

    endpoint: str
    access_key: SecretStr
    secret_key: SecretStr
    region: str = "us-east-1"
    bucket_recordings: str = "recordings"
    bucket_resumes: str = "resumes"


class _ProviderRoleSettings(BaseSettings):
    """某个角色使用的厂商。型号和地址不设默认值，空字符串表示该角色尚未配置。"""

    provider: str = ""
    model: str = ""
    base_url: str = ""
    timeout_s: float = Field(default=30.0, gt=0)

    @property
    def configured(self) -> bool:
        return bool(self.provider and self.model)


class LLMFrontSettings(_ProviderRoleSettings):
    model_config = _config("LLM_FRONT_")


class LLMEvalSettings(_ProviderRoleSettings):
    model_config = _config("LLM_EVAL_")


class LLMFallbackSettings(_ProviderRoleSettings):
    model_config = _config("LLM_FALLBACK_")


class STTSettings(_ProviderRoleSettings):
    model_config = _config("STT_")


class TTSSettings(_ProviderRoleSettings):
    model_config = _config("TTS_")


class ObservabilitySettings(BaseSettings):
    """链路追踪与指标的总开关。关闭时所有埋点工具都是空操作，不影响业务。"""

    model_config = _config("OBSERVABILITY_")

    enabled: bool = False
    sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)  # 按 trace 采样的比例


class MetricsSettings(BaseSettings):
    """worker 和评测进程独立的 Prometheus 指标端口（API 的 /metrics 挂在 API 自身端口上）。"""

    model_config = _config("")

    worker_metrics_port: int = Field(default=9465, gt=0, lt=65536)
    eval_metrics_port: int = Field(default=9466, gt=0, lt=65536)


class LangfuseSettings(BaseSettings):
    """自部署 Langfuse（OTLP 接收端）。OBSERVABILITY_ENABLED=true 时三项必填。"""

    model_config = _config("LANGFUSE_")

    host: str = ""
    public_key: str = ""
    secret_key: SecretStr = SecretStr("")

    @property
    def configured(self) -> bool:
        return bool(self.host and self.public_key and self.secret_key.get_secret_value())


class ProvidersSettings(BaseModel):
    llm_front: LLMFrontSettings
    llm_eval: LLMEvalSettings
    llm_fallback: LLMFallbackSettings
    stt: STTSettings
    tts: TTSSettings


class Settings(BaseModel):
    app: AppSettings
    db: DatabaseSettings
    redis: RedisSettings
    livekit: LiveKitSettings
    s3: S3Settings
    providers: ProvidersSettings
    observability: ObservabilitySettings
    langfuse: LangfuseSettings
    metrics: MetricsSettings


def _load[T: BaseSettings](cls: type[T], env_file: Path | None, missing: list[str]) -> T:
    """读取一组配置；出错时把缺失的环境变量名记入 missing，返回未校验的占位对象。"""
    try:
        return cls(_env_file=env_file)
    except ValidationError as exc:
        prefix = cls.model_config.get("env_prefix") or ""
        for err in exc.errors():
            field = ".".join(str(part) for part in err["loc"])
            missing.append(f"{(prefix + field).upper()}（{err['msg']}）")
        return cls.model_construct()


def load_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> Settings:
    """读取并校验全部配置；env_file=None 时只读环境变量（测试用）。"""
    errors: list[str] = []
    app = _load(AppSettings, env_file, errors)
    db = _load(DatabaseSettings, env_file, errors)
    redis = _load(RedisSettings, env_file, errors)
    livekit = _load(LiveKitSettings, env_file, errors)
    s3 = _load(S3Settings, env_file, errors)
    llm_front = _load(LLMFrontSettings, env_file, errors)
    llm_eval = _load(LLMEvalSettings, env_file, errors)
    llm_fallback = _load(LLMFallbackSettings, env_file, errors)
    stt = _load(STTSettings, env_file, errors)
    tts = _load(TTSSettings, env_file, errors)
    observability = _load(ObservabilitySettings, env_file, errors)
    langfuse = _load(LangfuseSettings, env_file, errors)
    metrics = _load(MetricsSettings, env_file, errors)
    if observability.enabled and not langfuse.configured:
        errors.append(
            "LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY"
            "（OBSERVABILITY_ENABLED=true 时必填）"
        )
    if errors:
        raise ConfigError("配置缺失或无效（见 .env.example）：\n  " + "\n  ".join(errors))
    return Settings(
        app=app,
        db=db,
        redis=redis,
        livekit=livekit,
        s3=s3,
        providers=ProvidersSettings(
            llm_front=llm_front,
            llm_eval=llm_eval,
            llm_fallback=llm_fallback,
            stt=stt,
            tts=tts,
        ),
        observability=observability,
        langfuse=langfuse,
        metrics=metrics,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内只读取一次；测试中如需重新读取，调用 get_settings.cache_clear()。"""
    return load_settings()
