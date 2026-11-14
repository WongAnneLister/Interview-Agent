"""把 STTProvider 包装成 LiveKit Agents 的流式 STT（livekit-agents 1.8.11）。

- 一条 RecognizeStream 对应整场面试的麦克风音频：基类把输入帧重采样成 16 kHz，
  这里转成 PCM16 字节交给厂商的流式识别。
- 支持手动 flush（manual_flush=True）：AgentSession 在 VAD 判定候选人停下时调用 flush()，
  插件随即结束当前厂商任务（Fun-ASR 收到 finish-task 立刻给出最终结果），下一段音频来了再开新任务。
  不 flush 时要等 Fun-ASR 自己判定句末，实测说完到出最终结果 2.5–13 秒（PR-29，C8）。
  每个任务的时间戳从 0 算，换任务时让基类按下一帧重新定锚。
- 事件映射（参照官方 Deepgram 插件）：一句话的第一个 partial 之前发 START_OF_SPEECH，
  partial → INTERIM_TRANSCRIPT，final → FINAL_TRANSCRIPT + END_OF_SPEECH，
  随后发一条 RECOGNITION_USAGE（本句新增的计费时长），基类据此上报 STTMetrics。
- 语种：厂商事件不带语种时用面试语言兜底（challenges C11），轮次检测要用它。
- 厂商错误翻译成 APIError，由基类按 retryable 重连重试。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Never

from livekit import rtc
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
    stt,
    utils,
)
from livekit.agents.language import LanguageCode

from interview.db.models import Language
from interview.providers.base import ProviderError, STTEvent, STTProvider
from interview.voice.plugin_errors import to_api_error

# 百炼 Fun-ASR 实时识别要求的输入采样率；LiveKit 的麦克风帧通常是 48 kHz，由基类重采样
SAMPLE_RATE = 16000


class ProviderSTT(stt.STT[Never]):  # 没有自定义事件
    def __init__(
        self,
        provider: STTProvider,
        *,
        language: Language,
        language_hint: str | None = None,
        hotword_list_id: str | None = None,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True, interim_results=True, offline_recognize=False, manual_flush=True
            )
        )
        self._provider = provider
        self._language = language
        self._language_hint = language_hint
        self._hotword_list_id = hotword_list_id
        self._warmup: asyncio.Task[None] | None = None

    @property
    def model(self) -> str:
        return self._provider.model

    @property
    def provider(self) -> str:
        return self._provider.provider

    def prewarm(self) -> None:
        """提前建好识别连接；失败只影响首包延迟，真正识别时会再连。"""
        if self._warmup is None or self._warmup.done():
            self._warmup = asyncio.create_task(self._provider.warmup(), name="stt_warmup")

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        raise NotImplementedError("只支持流式识别")

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> _Stream:
        return _Stream(self, conn_options=conn_options)

    async def aclose(self) -> None:
        if self._warmup is not None:
            await utils.aio.cancel_and_wait(self._warmup)


class _Stream(stt.RecognizeStream):
    def __init__(self, owner: ProviderSTT, *, conn_options: APIConnectOptions) -> None:
        # 基类参数标注为未指定类型参数的 STT，严格模式下只能忽略
        super().__init__(  # pyright: ignore[reportUnknownMemberType]
            stt=owner, conn_options=conn_options, sample_rate=SAMPLE_RATE
        )
        self._owner = owner
        self._speaking = False
        self._input_ended = False
        self._billed_s = 0.0  # 当前任务已经通过 RECOGNITION_USAGE 上报的计费时长

    async def _next_frame(self) -> rtc.AudioFrame | None:
        """下一段的第一帧；跳过没有音频的 flush，输入结束返回 None。"""
        while True:
            try:
                item = await self._input_ch.recv()
            except utils.aio.ChanClosed:
                return None
            if isinstance(item, rtc.AudioFrame):
                return item

    async def _segment(self, first: rtc.AudioFrame) -> AsyncIterator[bytes]:
        """一个厂商任务的音频：读到 flush 或输入结束为止。"""
        yield first.data.tobytes()
        while True:
            try:
                item = await self._input_ch.recv()
            except utils.aio.ChanClosed:
                self._input_ended = True
                return
            if not isinstance(item, rtc.AudioFrame):  # _FlushSentinel：候选人停下了
                return
            yield item.data.tobytes()

    async def _run(self) -> None:
        self._input_ended = False
        while not self._input_ended and (first := await self._next_frame()) is not None:
            await self._recognize(first)
            # 下一个任务的时间戳又从 0 算：让基类按下一帧重新定锚
            self._input_ch.restart_provider_clock()
        if self._speaking:  # 音频结束时还有没说完的半句
            self._speaking = False
            self._send(stt.SpeechEventType.END_OF_SPEECH)

    async def _recognize(self, first: rtc.AudioFrame) -> None:
        owner = self._owner
        self._billed_s = 0.0
        try:
            async with owner._provider.stream(  # pyright: ignore[reportPrivateUsage]
                self._segment(first),
                sample_rate=SAMPLE_RATE,
                language_hint=owner._language_hint,  # pyright: ignore[reportPrivateUsage]
                hotword_list_id=owner._hotword_list_id,  # pyright: ignore[reportPrivateUsage]
            ) as events:
                async for event in events:
                    self._handle(event)
                    if event.kind == "final":
                        self._report_usage(events.usage.audio_seconds if events.usage else 0.0)
            if self._speaking:  # 本段只有 partial 没有 final：也算说完
                self._speaking = False
                self._send(stt.SpeechEventType.END_OF_SPEECH)
            # 最后一句之后的音频（含静音）也计费，任务结束时补报
            self._report_usage(events.usage.audio_seconds if events.usage else 0.0)
        except ProviderError as exc:
            raise to_api_error(exc) from exc

    def _handle(self, event: STTEvent) -> None:
        if not event.text and event.kind == "partial":
            return
        if not self._speaking:
            self._speaking = True
            self._send(stt.SpeechEventType.START_OF_SPEECH)
        offset = self.start_time_offset
        data = stt.SpeechData(
            language=LanguageCode(event.language or self._owner._language.value),  # pyright: ignore[reportPrivateUsage]
            text=event.text,
            start_time=event.start_ms / 1000 + offset,
            end_time=event.end_ms / 1000 + offset,
        )
        if event.kind == "partial":
            self._send(stt.SpeechEventType.INTERIM_TRANSCRIPT, data)
            return
        self._send(stt.SpeechEventType.FINAL_TRANSCRIPT, data)
        self._speaking = False
        self._send(stt.SpeechEventType.END_OF_SPEECH)

    def _report_usage(self, total_s: float) -> None:
        delta = total_s - self._billed_s
        if delta <= 0:
            return
        self._billed_s = total_s
        self._event_ch.send_nowait(
            stt.SpeechEvent(
                type=stt.SpeechEventType.RECOGNITION_USAGE,
                recognition_usage=stt.RecognitionUsage(audio_duration=delta),
            )
        )

    def _send(self, kind: stt.SpeechEventType, data: stt.SpeechData | None = None) -> None:
        self._event_ch.send_nowait(
            stt.SpeechEvent(type=kind, alternatives=[data] if data is not None else [])
        )
