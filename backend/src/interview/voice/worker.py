"""语音 worker（livekit-agents 1.8.11）。

- build_server() 建 AgentServer：按配置的 agent_name 接 API 显式派发的任务（PR-26），
  LiveKit 地址和密钥取自配置，不依赖环境变量。CLI 入口在 voice/main.py，
  本模块导入时没有副作用，单元测试可以直接导入。
- prewarm() 在每个空闲进程里提前加载 Silero VAD、配置日志和链路追踪。
- entrypoint() 每场面试一个进程：解析派发 metadata，加载面试编排器，
  InterviewAgent 把候选人每一轮的最终转写交给 VoiceDriver（voice/driver.py），
  编排器吐出的句子经我们的合成插件说出来；不用 LiveKit 自带的大模型调用。
- 面试结束：driver 返回后 ctx.shutdown()，会话关闭并删除房间，候选人随之断开；
  on_session_end 钩子（会话关闭后、进程退出前，有 session_end_timeout）等报告写完。
  候选人中途离开：driver 被取消时 checkpoint() 存档，面试保持 in_progress（续面见 PR-39）。
- 这一版面试官说话时不能打断（allow_interruptions=False），候选人插话会被丢弃，
  历史记录与实际播出一致；打断见 PR-31。
- 优雅退出由 LiveKit CLI 完成（非 dev 模式）：收到停止信号后不再接新任务，
  最多等 drain_timeout_s 让进行中的面试结束。

livekit-agents 的 Agent / AgentSession 签名里有未指定类型参数的泛型（STT[Unknown] 等），
严格模式下调用它们的几处只能逐行忽略 reportUnknownMemberType。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import structlog
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    AgentStateChangedEvent,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    UserStateChangedEvent,
    llm,
    room_io,
    utils,
)
from livekit.agents.voice import SpeechHandle
from livekit.plugins import silero
from pydantic import ValidationError

from interview.config import Settings, get_settings
from interview.db.models import UsageRole
from interview.db.session import create_engine, make_sessionmaker
from interview.domain.orchestrator import InterviewOrchestrator
from interview.domain.orchestrator_store import InterviewNotFound
from interview.domain.report import ReportRunner, request_report
from interview.domain.state_machine import Phase
from interview.logging import configure_logging
from interview.observability.tracing import init_tracing
from interview.providers.observed import CallContext
from interview.providers.registry import ProviderRegistry
from interview.voice.driver import Playout, SentenceSink, SessionClock, Speaker, VoiceDriver
from interview.voice.session import DispatchMetadata
from interview.voice.stt_plugin import ProviderSTT
from interview.voice.tts_plugin import ProviderTTS

log = structlog.get_logger(__name__)

# 报告生成之外再给的余量（秒）：存档、等用量写库
SESSION_END_MARGIN_S = 60


class _HandlePlayout:
    def __init__(self, handle: SpeechHandle) -> None:
        self._handle = handle

    async def wait(self) -> None:
        await self._handle.wait_for_playout()


class _QueueSink:
    """一段边生成边说的话：句子进队列，session.say() 从队列里读。"""

    def __init__(self, session: AgentSession[None]) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._handle = session.say(  # pyright: ignore[reportUnknownMemberType]
            self._texts(), allow_interruptions=False
        )

    async def _texts(self) -> AsyncIterator[str]:
        while (sentence := await self._queue.get()) is not None:
            yield sentence

    def push(self, sentence: str) -> None:
        self._queue.put_nowait(sentence)

    def close(self) -> Playout:
        self._queue.put_nowait(None)
        return _HandlePlayout(self._handle)


class LiveKitSpeaker:
    def __init__(self, session: AgentSession[None]) -> None:
        self._session = session

    def say(self, text: str) -> Playout:
        handle = self._session.say(  # pyright: ignore[reportUnknownMemberType]
            text, allow_interruptions=False
        )
        return _HandlePlayout(handle)

    def stream(self) -> SentenceSink:
        return _QueueSink(self._session)


class InterviewAgent(Agent):
    def __init__(
        self,
        clock: SessionClock,
        *,
        make_driver: Callable[[Speaker], VoiceDriver],
        on_ended: Callable[[], None],
    ) -> None:
        super().__init__(instructions="interview")  # pyright: ignore[reportUnknownMemberType]
        self._clock = clock
        self._make_driver = make_driver
        self._on_ended = on_ended
        self._driver: VoiceDriver | None = None
        self._task: asyncio.Task[None] | None = None
        self._speech_started_ms: int | None = None
        self._stopped_at: float | None = None  # 候选人最近一次停下说话的墙钟时间，用来量延迟

    async def on_enter(self) -> None:
        session: AgentSession[None] = self.session  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        session.on("user_state_changed", self._on_user_state)  # pyright: ignore[reportUnknownMemberType]
        session.on("agent_state_changed", self._on_agent_state)  # pyright: ignore[reportUnknownMemberType]
        self._driver = self._make_driver(LiveKitSpeaker(session))
        self._task = asyncio.create_task(self._drive(self._driver), name="voice_driver")

    async def _drive(self, driver: VoiceDriver) -> None:
        if await driver.run():
            self._on_ended()

    def _on_user_state(self, event: UserStateChangedEvent) -> None:
        if event.new_state == "speaking" and self._speech_started_ms is None:
            self._speech_started_ms = self._clock.at(event.created_at)
        if event.old_state == "speaking" and event.new_state == "listening":
            self._stopped_at = event.created_at

    def _on_agent_state(self, event: AgentStateChangedEvent) -> None:
        # 一轮的体感延迟：候选人停下 → 面试官开口（过渡语或正文），C8 / PR-33
        if event.new_state == "speaking" and self._stopped_at is not None:
            log.info(
                "voice_reply_started",
                since_user_stopped_ms=round((event.created_at - self._stopped_at) * 1000),
            )
            self._stopped_at = None

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        text = (new_message.text_content or "").strip()
        started, self._speech_started_ms = self._speech_started_ms, None
        if not text or self._driver is None:
            return
        end_ms = self._clock.now_ms()
        if self._stopped_at is not None:
            log.info(
                "voice_turn_committed",
                since_user_stopped_ms=round((time.time() - self._stopped_at) * 1000),
            )
        # 交给 driver 就返回：评估和面试官生成在 driver 里跑，不挡住框架处理下一轮
        self._driver.user_turn(text, end_ms if started is None else started, end_ms)

    async def stop(self) -> None:
        """会话结束后调用：取消还在跑的 driver，等它存档（driver 的 finally）。"""
        if self._task is not None and not self._task.done():
            await utils.aio.cancel_and_wait(self._task)


def turn_handling(settings: Settings) -> TurnHandlingOptions:
    """候选人停顿多久算说完：框架默认 0.5 秒会在回答中途抢话，用配置值（默认 1.5 秒）。"""
    min_delay = settings.livekit.endpointing_min_delay_s
    return TurnHandlingOptions(
        endpointing={"min_delay": min_delay, "max_delay": max(3.0, min_delay)}
    )


@dataclass
class _JobState:
    """一个任务进程里 on_session_end 要收尾的对象（一个进程只跑一场面试）。"""

    agent: InterviewAgent | None
    reports: ReportRunner


def build_server(settings: Settings) -> AgentServer:
    livekit = settings.livekit
    server = AgentServer(
        ws_url=livekit.url,
        api_key=livekit.api_key.get_secret_value(),
        api_secret=livekit.api_secret.get_secret_value(),
        setup_fnc=prewarm,
        num_idle_processes=livekit.worker_idle_processes,
        drain_timeout=livekit.drain_timeout_s,
        session_end_timeout=settings.report.total_timeout_s + SESSION_END_MARGIN_S,
    )
    server.rtc_session(entrypoint, agent_name=livekit.agent_name, on_session_end=on_session_end)
    return server


def prewarm(proc: JobProcess) -> None:
    settings = get_settings()
    configure_logging(level=settings.app.log_level, fmt=settings.app.log_format)
    init_tracing(
        "worker", settings.observability, settings.langfuse, environment=settings.app.app_env
    )
    proc.userdata["vad"] = silero.VAD.load()


async def on_session_end(ctx: JobContext) -> None:
    job = ctx.proc.userdata.pop("job", None)
    if not isinstance(job, _JobState):
        return
    if job.agent is not None:
        await job.agent.stop()
    await job.reports.aclose()  # 面试结束时启动的报告：等它写完（最长 REPORT_TOTAL_TIMEOUT_S）


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
    sessionmaker = make_sessionmaker(engine)
    registry = ProviderRegistry(settings.providers, sessionmaker=sessionmaker)
    job = _JobState(agent=None, reports=ReportRunner(sessionmaker, registry, settings.report))
    ctx.proc.userdata["job"] = job

    async def close() -> None:
        await registry.aclose()  # 先等后台用量写完
        await engine.dispose()

    ctx.add_shutdown_callback(close)

    async def on_ended(interview_id: object) -> None:
        await request_report(meta.interview_id, user_id=meta.user_id, runner=job.reports)

    try:
        orchestrator = await InterviewOrchestrator.load(
            interview_id=meta.interview_id,
            user_id=meta.user_id,
            sessionmaker=sessionmaker,
            providers=registry,
            on_ended=on_ended,
        )
    except InterviewNotFound:
        log.error("voice_interview_not_found")
        ctx.shutdown("interview not found")
        return
    state = orchestrator.state
    if state is not None and state.phase is Phase.ENDED:
        await orchestrator.checkpoint()  # 上次进入 ended 后没来得及收尾：补做
        await orchestrator.aclose()
        ctx.shutdown("interview already ended")
        return

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

    clock = SessionClock(state.clock_ms if state is not None else 0)
    job.agent = InterviewAgent(
        clock,
        make_driver=lambda speaker: VoiceDriver(
            orchestrator,
            speaker,
            clock,
            language=meta.language,
            tick_interval_s=settings.app.ws_tick_interval_s,
        ),
        on_ended=lambda: ctx.shutdown("interview ended"),
    )
    session = AgentSession[None](
        vad=ctx.proc.userdata["vad"],
        stt=stt,
        tts=tts,
        turn_handling=turn_handling(settings),
        # 口语规范化（speech_text）已经去掉 markdown；框架的 markdown 过滤在流式文本里会先压住片段
        tts_text_transforms=["filter_emoji"],
    )
    # record=False：录音按 PR-34 存我们自己的对象存储，不交给框架
    await session.start(  # pyright: ignore[reportUnknownMemberType]
        job.agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(delete_room_on_close=True),
        record=False,
    )
    log.info(
        "voice_session_started",
        room=ctx.room.name,
        language=meta.language.value,
        resumed=state is not None,
    )
