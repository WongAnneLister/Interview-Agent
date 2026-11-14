"""统一读取配置：环境变量优先，其次是仓库根目录的 .env。

按模块分组，每组对应一个环境变量前缀（如 DATABASE_、S3_、LLM_FRONT_）。
必填项缺失或为空时，get_settings() 抛出 ConfigError 并列出所有缺失的环境变量名，
进程在启动阶段就会失败，而不是等到第一次用到时才出错。
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/interview/config.py → 仓库根目录
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_PRICING_FILE = REPO_ROOT / "backend" / "config" / "pricing.yaml"
DEFAULT_TURN_KEYWORDS_FILE = REPO_ROOT / "backend" / "config" / "turn_keywords.yaml"


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
    # v0 唯一的本地用户，由 make seed 创建并写入 .env
    default_user_id: UUID | None = None
    # 允许跨域访问 API 的前端地址（JSON 数组，如 ["http://localhost:3000"]）；为空时不允许任何跨域请求
    web_origins: list[str] = Field(default_factory=list[str])
    # /readyz 每项依赖检查的超时（秒）；经 SSH 隧道连 VPS 时新建连接要 10 秒左右，开发时调大
    ready_check_timeout_s: float = Field(default=3.0, gt=0)
    # 文字面试 WebSocket：这么多秒没收到客户端任何消息（含 ping）就视为断线；前端每 20 秒发一次 ping
    ws_idle_timeout_s: float = Field(default=60.0, gt=0)
    # 服务端推进超时（阶段、思考）的 tick 间隔（秒）
    ws_tick_interval_s: float = Field(default=1.0, gt=0)


class TurnSettings(BaseSettings):
    """语音面试的轮次策略（PR-30）：候选人停顿多久算说完，按面试官上一句的类型取值。

    min_delay：判定说完后再等多久；max_delay：轮次检测模型认为没说完时最多等多久
    （不用模型时只有 min_delay 起作用）。框架默认 0.5 秒会在开放回答的正常停顿处抢话（PR-29 实测）。
    """

    model_config = _config("TURN_")

    short_min_delay_s: float = Field(default=0.6, gt=0, le=10)  # 确认类短问题（准备好了吗）
    short_max_delay_s: float = Field(default=2.0, gt=0, le=20)
    open_min_delay_s: float = Field(default=1.5, gt=0, le=10)  # 开放回答
    open_max_delay_s: float = Field(default=4.0, gt=0, le=20)  # 没说完的 3 秒停顿不被抢话
    think_timeout_s: float = Field(default=20.0, gt=0)  # 说“我想一下”后最多等多久再询问
    detector_enabled: bool = True  # 轮次检测模型（约 400 MB，make download-models）
    keywords_file: Path = DEFAULT_TURN_KEYWORDS_FILE  # “我想一下 / 我说完了”词表

    @model_validator(mode="after")
    def max_not_below_min(self) -> TurnSettings:
        if self.short_max_delay_s < self.short_min_delay_s:
            raise ValueError("TURN_SHORT_MAX_DELAY_S 不能小于 TURN_SHORT_MIN_DELAY_S")
        if self.open_max_delay_s < self.open_min_delay_s:
            raise ValueError("TURN_OPEN_MAX_DELAY_S 不能小于 TURN_OPEN_MIN_DELAY_S")
        return self


class ReportSettings(BaseSettings):
    """面试后的报告评定：按题并发、重试与整份报告的时限。"""

    model_config = _config("REPORT_")

    concurrency: int = Field(default=4, gt=0)
    question_timeout_s: float = Field(default=40.0, gt=0)
    question_attempts: int = Field(default=2, gt=0)
    total_timeout_s: float = Field(default=90.0, gt=0)


class DatabaseSettings(BaseSettings):
    """数据库连接。开发库经 SSH 隧道访问时建连可能要 10 秒以上，可在 .env 放宽（C23）。"""

    model_config = _config("DATABASE_")

    url: SecretStr  # 连接串里含密码
    connect_timeout_s: float = Field(default=10.0, gt=0)  # 建立连接的超时
    command_timeout_s: float = Field(default=30.0, gt=0)  # 单条语句的超时
    pool_size: int = Field(default=5, ge=1)  # 常驻连接数
    max_overflow: int = Field(default=10, ge=0)  # 临时连接数，用完即关


class RedisSettings(BaseSettings):
    model_config = _config("REDIS_")

    url: SecretStr


class LiveKitSettings(BaseSettings):
    model_config = _config("LIVEKIT_")

    url: str  # 浏览器与 worker 都用它连 LiveKit（v0 都在本机）
    api_key: SecretStr
    api_secret: SecretStr
    # 语音 worker 注册的名字；入房 token 里按这个名字显式派发 agent
    agent_name: str = "interview-agent"
    # 候选人入房 token 的有效期（秒）：只用于建立连接，入房后过期不影响通话
    token_ttl_s: int = Field(default=600, gt=0, le=3600)
    # 语音 worker 预先启动的空闲进程数；框架生产默认是 CPU 核数，本机开这么多会重复加载 Silero
    worker_idle_processes: int = Field(default=1, ge=0)
    # worker 收到停止信号后等进行中的面试结束的最长时间（秒）：最长 30 分钟的面试加余量
    drain_timeout_s: int = Field(default=2400, gt=0)


class S3Settings(BaseSettings):
    """S3 兼容对象存储（开发用 SeaweedFS，生产可换 OSS）。"""

    model_config = _config("S3_")

    endpoint: str
    access_key: SecretStr
    secret_key: SecretStr
    region: str = "us-east-1"
    bucket_recordings: str = "recordings"
    bucket_resumes: str = "resumes"


class ProviderRoleSettings(BaseSettings):
    """某个角色使用的厂商。型号和地址不设默认值，空字符串表示该角色尚未配置。"""

    provider: str = ""
    model: str = ""
    base_url: str = ""  # 留空时用厂商级的 <厂商>_BASE_URL
    # 单次调用的读超时：流式响应里是两个片段之间的最长间隔，不限制总时长
    timeout_s: float = Field(default=30.0, gt=0)
    # 大模型 complete() 一次调用的总时限（秒）：开思考时片段不断，只有它能拦住跑几分钟的调用（C31）
    total_timeout_s: float = Field(default=120.0, gt=0)
    # 透传给厂商的非标准参数（JSON），如百炼的 enable_thinking；与调用时的 extra 合并，调用时优先
    extra_body: dict[str, Any] = Field(default_factory=dict[str, Any])

    @property
    def configured(self) -> bool:
        return bool(self.provider and self.model)


class LLMFrontSettings(ProviderRoleSettings):
    model_config = _config("LLM_FRONT_")

    # 前台要低延迟，默认关闭思考（百炼参数）；配置了 LLM_FRONT_EXTRA_BODY 时整体替换
    extra_body: dict[str, Any] = Field(default_factory=lambda: {"enable_thinking": False})


class LLMEvalSettings(ProviderRoleSettings):
    model_config = _config("LLM_EVAL_")


class LLMLiveEvalSettings(ProviderRoleSettings):
    """面试中的实时评估：要在 2.5 秒内决定追问还是换题，选快的型号（实测见 challenges.md C1）。"""

    model_config = _config("LLM_LIVE_EVAL_")


class LLMReportSettings(ProviderRoleSettings):
    """面试后的报告评分与汇总。PRD R1 要求 60 秒内出报告，开思考的 max 要 74–82 秒（C20）。"""

    model_config = _config("LLM_REPORT_")


class LLMFallbackSettings(ProviderRoleSettings):
    model_config = _config("LLM_FALLBACK_")

    # 备用计划用 DeepSeek（默认开启思考），备用时优先保证延迟，默认关闭（DeepSeek 参数）
    extra_body: dict[str, Any] = Field(default_factory=lambda: {"thinking": {"type": "disabled"}})


class VendorSettings(BaseSettings):
    """厂商级的密钥和默认接口地址，多个角色共用（同一个百炼密钥也用于识别和合成）。"""

    api_key: SecretStr = SecretStr("")
    base_url: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key.get_secret_value())


class BailianSettings(VendorSettings):
    model_config = _config("BAILIAN_")

    # 原生 API 地址（热词表等非 OpenAI 兼容接口），与 BASE_URL（OpenAI 兼容地址）不同
    api_base_url: str = ""


class DeepSeekSettings(VendorSettings):
    model_config = _config("DEEPSEEK_")


class STTSettings(ProviderRoleSettings):
    model_config = _config("STT_")


class TTSSettings(ProviderRoleSettings):
    model_config = _config("TTS_")

    voice: str = ""  # 默认音色，调用时传入的 voice 优先


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


class CostSettings(BaseSettings):
    """用量成本估算与预算保护（金额单位：人民币元）。"""

    model_config = _config("")

    pricing_file: Path = DEFAULT_PRICING_FILE
    max_cost_per_interview_cny: Decimal = Field(default=Decimal(5), gt=0)  # 单场预算
    daily_budget_cny: Decimal = Field(default=Decimal(50), gt=0)  # 每日全部用户、不含评测


class ProvidersSettings(BaseModel):
    llm_front: LLMFrontSettings
    llm_eval: LLMEvalSettings
    llm_fallback: LLMFallbackSettings
    stt: STTSettings
    tts: TTSSettings
    # 给默认值：未配置时视为没有这个角色，调用方回退到 llm_eval；只构造了旧字段的调用方不受影响
    llm_live_eval: LLMLiveEvalSettings = Field(
        default_factory=lambda: LLMLiveEvalSettings.model_construct()
    )
    llm_report: LLMReportSettings = Field(
        default_factory=lambda: LLMReportSettings.model_construct()
    )
    bailian: BailianSettings = Field(default_factory=lambda: BailianSettings.model_construct())
    deepseek: DeepSeekSettings = Field(default_factory=lambda: DeepSeekSettings.model_construct())


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
    cost: CostSettings
    report: ReportSettings = Field(default_factory=ReportSettings)
    turn: TurnSettings = Field(default_factory=TurnSettings)


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
    llm_live_eval = _load(LLMLiveEvalSettings, env_file, errors)
    llm_report = _load(LLMReportSettings, env_file, errors)
    llm_fallback = _load(LLMFallbackSettings, env_file, errors)
    stt = _load(STTSettings, env_file, errors)
    tts = _load(TTSSettings, env_file, errors)
    bailian = _load(BailianSettings, env_file, errors)
    deepseek = _load(DeepSeekSettings, env_file, errors)
    observability = _load(ObservabilitySettings, env_file, errors)
    langfuse = _load(LangfuseSettings, env_file, errors)
    metrics = _load(MetricsSettings, env_file, errors)
    cost = _load(CostSettings, env_file, errors)
    report = _load(ReportSettings, env_file, errors)
    turn = _load(TurnSettings, env_file, errors)
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
            llm_live_eval=llm_live_eval,
            llm_report=llm_report,
            llm_fallback=llm_fallback,
            stt=stt,
            tts=tts,
            bailian=bailian,
            deepseek=deepseek,
        ),
        observability=observability,
        langfuse=langfuse,
        metrics=metrics,
        cost=cost,
        report=report,
        turn=turn,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内只读取一次；测试中如需重新读取，调用 get_settings.cache_clear()。"""
    return load_settings()
