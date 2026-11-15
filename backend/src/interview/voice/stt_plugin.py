"""把 STTProvider 包装成 LiveKit Agents 的流式 STT（livekit-agents 1.8.11）。

- 一条 RecognizeStream 对应整场面试的麦克风音频：基类把输入帧重采样成 16 kHz，
  这里转成 PCM16 字节交给厂商的流式识别。
- 支持手动 flush（manual_flush=True）：AgentSession 在 VAD 判定候选人停下时调用 flush()，
  插件随即结束当前厂商任务（Fun-ASR 收到 finish-task 立刻给出最终结果），下一段音频来了再开新任务。
  不 flush 时要等 Fun-ASR 自己判定句末，实测说完到出最终结果 2.5–13 秒（PR-29，C8）。
  每个任务的时间戳从 0 算，换任务时让基类按下一帧重新定锚。
- flush 之后最多等 FLUSH_FINAL_WAIT_S（0.3 秒）拿最终结果，等不到就把最近一次中间结果
  当作最终结果、放弃这个任务；任务出错时也一样先保住中间结果再抛错。
  flush 时一个字都还没识别出来（短回答，Fun-ASR 还没来得及出中间结果）则最多等
  FLUSH_EMPTY_WAIT_S（8 秒）：这一轮没有文字时 LiveKit 不会结束这一轮，晚到的最终结果
  会重新触发轮次判断；0.3 秒就放弃的话，这句话被整段丢掉，面试官一直等（C41 后的实测，C42）。
  实测长回答里 Fun-ASR 一直不断句，finish-task 之后十几秒不返回，判定说完被拖到 6–10 秒，
  超时后整段识别结果被丢弃，候选人说的话等于没说（PR-30 实测）。
- 段首静音不发：上一段 flush 之后的帧（面试官说话、候选人还没开口）先攒在一个 0.3 秒的
  预录缓冲里，音量持续 SPEECH_MIN_S 超过 SPEECH_RMS 才开新任务，并带上缓冲免得吞掉第一个字。
  Fun-ASR 大致按实时速度处理音频，段首十几秒静音会让结果晚同样长的时间；
  静音也按时长计费（C42）。
- 事件映射（参照官方 Deepgram 插件）：一句话的第一个 partial 之前发 START_OF_SPEECH，
  partial → INTERIM_TRANSCRIPT，final → FINAL_TRANSCRIPT + END_OF_SPEECH，
  随后发一条 RECOGNITION_USAGE（本句新增的计费时长），基类据此上报 STTMetrics。
- 语种：厂商事件不带语种时用面试语言兜底（challenges C11），轮次检测要用它。
- 厂商错误翻译成 APIError，由基类按 retryable 重连重试。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Never

import numpy as np
import structlog
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
from interview.voice.debug_audio import SegmentRecorder
from interview.voice.plugin_errors import to_api_error

log = structlog.get_logger(__name__)

# 百炼 Fun-ASR 实时识别要求的输入采样率；LiveKit 的麦克风帧通常是 48 kHz，由基类重采样
SAMPLE_RATE = 16000
# flush 后等最终结果的上限（秒）。这一轮已经有文字时，LiveKit 不等 flush 的结果，
# 停顿 min_delay（短问题 0.6 秒）后就结束这一轮；晚到的文字会被算进下一轮，
# 面试官就总在回应上一轮（PR-30 实测，原来等 1.5 秒）。所以要远小于 min_delay；
# Fun-ASR 的最终结果实测几乎从没在 1.5 秒内回来过，等于立即交出中间结果。
FLUSH_FINAL_WAIT_S = 0.3
# flush 时还没有任何文字时的等待上限（秒）。录音重放实测：音频结束后 2.6–6.3 秒才出最终结果（C42）
FLUSH_EMPTY_WAIT_S = 8.0
# 段首判定“开口了”：音量（16 bit PCM 的均方根）持续 SPEECH_MIN_S 超过 SPEECH_RMS。
# 用 2026-11-15 的 12 段录音校准：10 毫秒级的底噪尖峰可达 199，单帧过 200 就开会提前十几秒；
# 400 且持续 50 毫秒时，每段都在开口后 0–20 毫秒内触发，没有误触发
SPEECH_RMS = 400
SPEECH_MIN_S = 0.05
# 开口前多带的音频（秒），保住第一个字的起音
PREROLL_S = 0.3


def _rms(frame: rtc.AudioFrame) -> float:
    samples = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0


class ProviderSTT(stt.STT[Never]):  # 没有自定义事件
    def __init__(
        self,
        provider: STTProvider,
        *,
        language: Language,
        language_hint: str | None = None,
        hotword_list_id: str | None = None,
        debug_dir: Path | None = None,
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
        self._debug_dir = debug_dir  # 排查用：每段音频存成 wav（voice/debug_audio.py）
        self._segments = 0
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
        self._partial: STTEvent | None = None  # 当前这句最近的中间结果，还没有对应的最终结果
        self._deadline: asyncio.Timeout | None = None  # flush 后才开始计时
        self._wait_s = FLUSH_FINAL_WAIT_S  # 本次 flush 实际等多久，记日志用
        self._heard = False  # 当前任务是否已经交出过文字（中间或最终结果）
        self._recorder: SegmentRecorder | None = None

    async def _next_frame(self) -> list[rtc.AudioFrame] | None:
        """下一段开头的几帧：开口前的预录缓冲 + 第一帧有声音的帧。

        跳过没有音频的 flush 和段首静音；输入结束返回 None。
        """
        frames: deque[rtc.AudioFrame] = deque()
        buffered = loud = 0.0
        while True:
            try:
                item = await self._input_ch.recv()
            except utils.aio.ChanClosed:
                return None
            if not isinstance(item, rtc.AudioFrame):
                continue
            frames.append(item)
            buffered += item.duration
            loud = loud + item.duration if _rms(item) >= SPEECH_RMS else 0.0
            if loud >= SPEECH_MIN_S:
                return list(frames)
            # 只留开口前 PREROLL_S（加上正在累计的这段响声）
            while frames and buffered - frames[0].duration >= PREROLL_S + loud:
                buffered -= frames.popleft().duration

    async def _segment(self, first: list[rtc.AudioFrame]) -> AsyncIterator[bytes]:
        """一个厂商任务的音频：读到 flush 或输入结束为止。"""
        for frame in first:
            self._record(frame.data.tobytes())
            yield frame.data.tobytes()
        while True:
            try:
                item = await self._input_ch.recv()
            except utils.aio.ChanClosed:
                self._input_ended = True
                self._start_final_deadline()
                return
            if not isinstance(item, rtc.AudioFrame):  # _FlushSentinel：候选人停下了
                self._note("flush")
                self._start_final_deadline()
                return
            self._record(item.data.tobytes())
            yield item.data.tobytes()

    def _record(self, chunk: bytes) -> None:
        if self._recorder is not None:
            self._recorder.audio(chunk)

    def _note(self, kind: str, **fields: Any) -> None:
        if self._recorder is not None:
            self._recorder.event(kind, **fields)

    def _start_final_deadline(self) -> None:
        if self._deadline is not None:
            # 已有文字：LiveKit 很快会结束这一轮，晚交会错位到下一轮（C38）；
            # 还没有文字：LiveKit 在等这句话，宁可多等也不能丢（C42）
            self._wait_s = FLUSH_FINAL_WAIT_S if self._heard else FLUSH_EMPTY_WAIT_S
            loop = asyncio.get_running_loop()
            self._deadline.reschedule(loop.time() + self._wait_s)

    async def _run(self) -> None:
        self._input_ended = False
        while not self._input_ended and (first := await self._next_frame()) is not None:
            await self._recognize(first)
            # 下一个任务的时间戳又从 0 算：让基类按下一帧重新定锚
            self._input_ch.restart_provider_clock()
        if self._speaking:  # 音频结束时还有没说完的半句
            self._speaking = False
            self._send(stt.SpeechEventType.END_OF_SPEECH)

    async def _recognize(self, first: list[rtc.AudioFrame]) -> None:
        owner = self._owner
        self._billed_s = 0.0
        self._partial = None
        self._heard = False
        if owner._debug_dir is not None:  # pyright: ignore[reportPrivateUsage]
            owner._segments += 1  # pyright: ignore[reportPrivateUsage]
            self._recorder = SegmentRecorder(
                owner._debug_dir,  # pyright: ignore[reportPrivateUsage]
                owner._segments,  # pyright: ignore[reportPrivateUsage]
                sample_rate=SAMPLE_RATE,
            )
        try:
            async with owner._provider.stream(  # pyright: ignore[reportPrivateUsage]
                self._segment(first),
                sample_rate=SAMPLE_RATE,
                language_hint=owner._language_hint,  # pyright: ignore[reportPrivateUsage]
                hotword_list_id=owner._hotword_list_id,  # pyright: ignore[reportPrivateUsage]
            ) as events:
                deadline = asyncio.timeout(None)
                self._deadline = deadline
                try:
                    async with deadline:
                        async for event in events:
                            self._handle(event)
                            if event.kind == "final":
                                self._report_usage(
                                    events.usage.audio_seconds if events.usage else 0.0
                                )
                except TimeoutError:
                    if not deadline.expired():  # 厂商自己的超时（ProviderTimeout）照常往外抛
                        raise
                    # 退出 async with 时关闭厂商流，适配器放弃这个任务
                    log.warning("stt_final_wait_expired", wait_s=self._wait_s)
                    self._note("final_wait_expired", wait_s=self._wait_s)
                    self._promote_partial()
                finally:
                    self._deadline = None
            if self._speaking:  # 本段只有 partial 没有 final：也算说完
                self._speaking = False
                self._send(stt.SpeechEventType.END_OF_SPEECH)
            # 最后一句之后的音频（含静音）也计费，任务结束时补报
            self._report_usage(events.usage.audio_seconds if events.usage else 0.0)
        except ProviderError as exc:
            self._note("error", error=type(exc).__name__)
            self._promote_partial()  # 任务失败也不丢已经识别出来的话
            raise to_api_error(exc) from exc
        finally:
            if self._recorder is not None:
                recorder, self._recorder = self._recorder, None
                await recorder.save()

    def _promote_partial(self) -> None:
        partial, self._partial = self._partial, None
        self._note("promoted", text=partial.text if partial is not None else "")
        if partial is not None:
            self._handle(
                STTEvent(
                    kind="final",
                    text=partial.text,
                    start_ms=partial.start_ms,
                    end_ms=partial.end_ms,
                    language=partial.language,
                )
            )

    def _handle(self, event: STTEvent) -> None:
        self._note(event.kind, text=event.text, start_ms=event.start_ms, end_ms=event.end_ms)
        if not event.text and event.kind == "partial":
            return
        self._heard = self._heard or bool(event.text)
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
            self._partial = event
            self._send(stt.SpeechEventType.INTERIM_TRANSCRIPT, data)
            return
        self._partial = None
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
