"""配置读取：必填缺失时报错并列出变量名、空值视为缺失、厂商默认未配置。"""

from pathlib import Path

import pytest

from interview.config import ConfigError, get_settings, load_settings

REQUIRED = {
    "DATABASE_URL": "postgresql+asyncpg://u:pw@127.0.0.1:5432/db",
    "REDIS_URL": "redis://:pw@127.0.0.1:6379/0",
    "LIVEKIT_URL": "ws://127.0.0.1:7880",
    "LIVEKIT_API_KEY": "lk-key",
    "LIVEKIT_API_SECRET": "lk-secret",
    "S3_ENDPOINT": "http://127.0.0.1:9000",
    "S3_ACCESS_KEY": "s3-access",
    "S3_SECRET_KEY": "s3-secret",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """不受开发者 shell 里已有变量的影响。"""
    for name in REQUIRED:
        monkeypatch.delenv(name, raising=False)
    for name in (
        "APP_ENV",
        "LOG_LEVEL",
        "LOG_FORMAT",
        "LLM_FRONT_MODEL",
        "LLM_FRONT_PROVIDER",
        "OBSERVABILITY_ENABLED",
        "OBSERVABILITY_SAMPLE_RATIO",
        "LANGFUSE_HOST",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "REPORT_CONCURRENCY",
        "REPORT_QUESTION_TIMEOUT_S",
        "REPORT_QUESTION_ATTEMPTS",
        "REPORT_TOTAL_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)


def set_required(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)


def test_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required(monkeypatch)
    settings = load_settings(env_file=None)
    assert settings.db.url.get_secret_value() == REQUIRED["DATABASE_URL"]
    assert settings.livekit.url == "ws://127.0.0.1:7880"
    assert settings.s3.bucket_recordings == "recordings"
    assert settings.app.log_format == "json"


def test_secrets_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required(monkeypatch)
    text = repr(load_settings(env_file=None))
    assert "lk-secret" not in text
    assert "s3-secret" not in text
    assert ":pw@" not in text


def test_missing_required_lists_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required(monkeypatch)
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("S3_SECRET_KEY")
    with pytest.raises(ConfigError) as exc_info:
        load_settings(env_file=None)
    message = str(exc_info.value)
    assert "DATABASE_URL" in message
    assert "S3_SECRET_KEY" in message
    assert "REDIS_URL" not in message


def test_empty_value_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required(monkeypatch)
    monkeypatch.setenv("LIVEKIT_API_KEY", "")
    with pytest.raises(ConfigError, match="LIVEKIT_API_KEY"):
        load_settings(env_file=None)


def test_reads_env_file_and_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_required(monkeypatch)
    monkeypatch.delenv("REDIS_URL")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "REDIS_URL=redis://from-file:6379/0\nS3_ENDPOINT=http://from-file\nVPS_HOST=ignored\n",
        encoding="utf-8",
    )
    settings = load_settings(env_file=env_file)
    assert settings.redis.url.get_secret_value() == "redis://from-file:6379/0"
    assert settings.s3.endpoint == REQUIRED["S3_ENDPOINT"]


def test_invalid_value_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required(monkeypatch)
    monkeypatch.setenv("LOG_FORMAT", "xml")
    with pytest.raises(ConfigError, match="LOG_FORMAT"):
        load_settings(env_file=None)


def test_providers_unconfigured_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required(monkeypatch)
    providers = load_settings(env_file=None).providers
    assert providers.llm_front.model == ""
    assert not providers.llm_front.configured
    monkeypatch.setenv("LLM_FRONT_PROVIDER", "bailian")
    monkeypatch.setenv("LLM_FRONT_MODEL", "some-model")
    assert load_settings(env_file=None).providers.llm_front.configured


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required(monkeypatch)
    get_settings.cache_clear()
    monkeypatch.setattr("interview.config.DEFAULT_ENV_FILE", None)
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()


def test_observability_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required(monkeypatch)
    settings = load_settings(env_file=None)
    assert settings.observability.enabled is False
    assert settings.observability.sample_ratio == 1.0
    assert not settings.langfuse.configured


def test_observability_enabled_requires_langfuse(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required(monkeypatch)
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:23100")
    with pytest.raises(ConfigError, match="LANGFUSE_SECRET_KEY"):
        load_settings(env_file=None)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-1")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-1")
    settings = load_settings(env_file=None)
    assert settings.langfuse.configured
    assert "sk-lf-1" not in repr(settings)


def test_sample_ratio_out_of_range(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required(monkeypatch)
    monkeypatch.setenv("OBSERVABILITY_SAMPLE_RATIO", "1.5")
    with pytest.raises(ConfigError, match="OBSERVABILITY_SAMPLE_RATIO"):
        load_settings(env_file=None)


def test_report_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required(monkeypatch)
    monkeypatch.setenv("REPORT_CONCURRENCY", "2")
    monkeypatch.setenv("REPORT_QUESTION_TIMEOUT_S", "12.5")
    monkeypatch.setenv("REPORT_QUESTION_ATTEMPTS", "3")
    monkeypatch.setenv("REPORT_TOTAL_TIMEOUT_S", "65")
    settings = load_settings(env_file=None)
    assert settings.report.concurrency == 2
    assert settings.report.question_timeout_s == 12.5
    assert settings.report.question_attempts == 3
    assert settings.report.total_timeout_s == 65


@pytest.mark.parametrize(
    "name",
    [
        "REPORT_CONCURRENCY",
        "REPORT_QUESTION_TIMEOUT_S",
        "REPORT_QUESTION_ATTEMPTS",
        "REPORT_TOTAL_TIMEOUT_S",
    ],
)
def test_report_settings_require_positive_values(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_required(monkeypatch)
    monkeypatch.setenv(name, "0")
    with pytest.raises(ConfigError, match=name):
        load_settings(env_file=None)
