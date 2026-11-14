"""语音 worker（livekit-agents 1.8.11）。

- build_server() 建 AgentServer：按配置的 agent_name 接 API 显式派发的任务（PR-26），
  LiveKit 地址和密钥取自配置，不依赖环境变量。CLI 入口在 voice/main.py，
  本模块导入时没有副作用，单元测试可以直接导入。
- prewarm() 在每个空闲进程里提前加载 Silero VAD、配置日志和链路追踪。
- entrypoint() 每场面试一个进程：解析派发 metadata，绑定 interview_id 到日志上下文，
  用我们的识别 / 合成插件建 AgentSession。现在跑的是回声 agent：候选人说完一句，
  就用合成声音复述识别到的文字（PR-28 验收用，PR-29 换成面试编排）。
- 优雅退出由 LiveKit CLI 完成（非 dev 模式）：收到停止信号后不再接新任务，
  最多等 drain_timeout_s 让进行中的面试结束。
"""

from __future__ import annotations

import structlog
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, JobProcess, llm
from livekit.plugins import silero
from pydantic import ValidationError

from interview.config import Settings, get_settings
from interview.db.models import Language, UsageRole
from interview.db.session import create_engine, make_sessionmaker
from interview.logging import configure_logging
from interview.observability.tracing import init_tracing
from interview.providers.observed import CallContext
from interview.providers.registry import ProviderRegistry
from interview.voice.session import DispatchMetadata
from interview.voice.stt_plugin import ProviderSTT
from interview.voice.tts_plugin import ProviderTTS

log = structlog.get_logger(__name__)

GREETINGS = {
    Language.ZH: "回声测试已连接，请说一句话，我会复述给你听。",
    Language.EN: "Echo test connected. Say something and I will repeat it.",
}


class EchoAgent(Agent):
    """把识别结果原样说回去，用来验证 识别 → 合成 整条链路。

    livekit-agents 的 Agent / AgentSession 签名里有未指定类型参数的泛型（STT[Unknown] 等），
    严格模式下下面几处只能逐行忽略 reportUnknownMemberType。
    """

    def __init__(self, language: Language) -> None:
        super().__init__(instructions="echo")  # pyright: ignore[reportUnknownMemberType]
        self._language = language

    async def on_enter(self) -> None:
        self.session.say(GREETINGS[self._language])  # pyright: ignore[reportUnknownMemberType]

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        text = (new_message.text_content or "").strip()
        if text:
            self.session.say(text)  # pyright: ignore[reportUnknownMemberType]


def build_server(settings: Settings) -> AgentServer:
    livekit = settings.livekit
    server = AgentServer(
        ws_url=livekit.url,
        api_key=livekit.api_key.get_secret_value(),
        api_secret=livekit.api_secret.get_secret_value(),
        setup_fnc=prewarm,
        num_idle_processes=livekit.worker_idle_processes,
        drain_timeout=livekit.drain_timeout_s,
    )
    server.rtc_session(entrypoint, agent_name=livekit.agent_name)
    return server


def prewarm(proc: JobProcess) -> None:
    settings = get_settings()
    configure_logging(level=settings.app.log_level, fmt=settings.app.log_format)
    init_tracing(
        "worker", settings.observability, settings.langfuse, environment=settings.app.app_env
    )
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    try:
        meta = DispatchMetadata.parse(ctx.job.metadata)
    except ValidationError:
        # 不记录 metadata 原文：只可能来自我们签发的 token，格式不对说明版本不一致
        log.error("voice_dispatch_metadata_invalid", room=ctx.room.name)
        ctx.shutdown("invalid dispatch metadata")
        return
    structlog.contextvars.bind_contextvars(interview_id=str(meta.interview_id))

    settings = get_settings()
    engine = create_engine(settings.db.url.get_secret_value(), db=settings.db)
    registry = ProviderRegistry(settings.providers, sessionmaker=make_sessionmaker(engine))

    async def close() -> None:
        await registry.aclose()  # 先等后台用量写完
        await engine.dispose()

    ctx.add_shutdown_callback(close)

    def call(role: UsageRole) -> CallContext:
        return CallContext(user_id=meta.user_id, interview_id=meta.interview_id, usage_role=role)

    stt = ProviderSTT(registry.stt(call(UsageRole.ASR)), language=meta.language)
    tts = ProviderTTS(
        registry.tts(call(UsageRole.TTS)),
        language=meta.language,
        voice=settings.providers.tts.voice or None,
    )
    # 进房前建好厂商连接，避免把建连时间算进第一句的延迟（C8）
    stt.prewarm()
    tts.prewarm()

    session = AgentSession[None](vad=ctx.proc.userdata["vad"], stt=stt, tts=tts)
    # record=False：录音按 PR-34 存我们自己的对象存储，不交给框架
    await session.start(  # pyright: ignore[reportUnknownMemberType]
        EchoAgent(meta.language), room=ctx.room, record=False
    )
    log.info("voice_session_started", room=ctx.room.name, language=meta.language.value)
