"""把 TTSProvider 包装成 LiveKit Agents 的流式 TTS（livekit-agents 1.8.11）。

- 一条 SynthesizeStream 对应面试官的一段话，只有一个 segment（参照官方 Cartesia 插件）；
  基类在 _run 结束后调用 end_input()，由它结束这个 segment。
- 推进来的文本先用项目自己的 SentenceSplitter 分句（LiveKit 自带的分句规则是给英文写的），
  每句过一遍 speech_text.normalize() 再交给厂商：单位、百分比、缩写这些规则要整句才能做对（C13）。
  第一句交出时调用 _mark_started()，基类据此算首包耗时（ttfb）并发出 TTSMetrics。
- 打断时 LiveKit 取消任务，async with 退出时调用厂商流的 aclose()，由适配器结束合成任务（C9）。
- synthesize() 用基类的 _synthesize_with_stream，整段文本也走同一条流式实现。
- 厂商错误翻译成 APIError；基类只在还没有播出任何音频时重试。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Never

from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, tts, utils

from interview.db.models import Language
from interview.domain.interviewer import SentenceSplitter
from interview.domain.speech_text import normalize
from interview.providers.base import ProviderError, TTSProvider
from interview.voice.plugin_errors import to_api_error

SAMPLE_RATE = 24000  # 百炼两种合成协议都用 24 kHz PCM16 单声道
NUM_CHANNELS = 1


class ProviderTTS(tts.TTS[Never]):  # 没有自定义事件
    def __init__(
        self,
        provider: TTSProvider,
        *,
        language: Language,
        voice: str | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
        )
        self._provider = provider
        self._language = language
        self._voice = voice
        self._warmup: asyncio.Task[None] | None = None

    @property
    def model(self) -> str:
        return self._provider.model

    @property
    def provider(self) -> str:
        return self._provider.provider

    def prewarm(self) -> None:
        """提前建好合成连接（C8）；失败只影响首包延迟，真正合成时会再连。"""
        if self._warmup is None or self._warmup.done():
            self._warmup = asyncio.create_task(self._provider.warmup(), name="tts_warmup")

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return self._synthesize_with_stream(text, conn_options=conn_options)

    def stream(self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> _Stream:
        return _Stream(self, conn_options=conn_options)

    async def aclose(self) -> None:
        if self._warmup is not None:
            await utils.aio.cancel_and_wait(self._warmup)


class _Stream(tts.SynthesizeStream):
    def __init__(self, owner: ProviderTTS, *, conn_options: APIConnectOptions) -> None:
        # 基类参数标注为未指定类型参数的 TTS，严格模式下只能忽略
        super().__init__(  # pyright: ignore[reportUnknownMemberType]
            tts=owner, conn_options=conn_options
        )
        self._owner = owner

    async def _sentences(self) -> AsyncIterator[str]:
        language = self._owner._language  # pyright: ignore[reportPrivateUsage]
        # 分句器会去掉句子之间的空格；英文句子要补回一个，否则会连读成 "Hi.How are you?"
        separator = " " if language == Language.EN else ""
        splitter = SentenceSplitter()
        # 每次重试基类都会换一条新的 _input_ch 并重放已推入的文本，所以每次 _run 都从头读
        async for item in self._input_ch:
            if isinstance(item, tts.SynthesizeStream._FlushSentinel):
                pieces = splitter.flush()
            else:
                pieces = splitter.feed(item)
            for sentence in pieces:
                if speakable := normalize(sentence, language):
                    self._mark_started()
                    yield speakable + separator
        for sentence in splitter.flush():
            if speakable := normalize(sentence, language):
                self._mark_started()
                yield speakable + separator

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        owner = self._owner
        request_id = utils.shortuuid()
        output_emitter.initialize(
            request_id=request_id,
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            mime_type="audio/pcm",
            stream=True,
        )
        output_emitter.start_segment(segment_id=request_id)
        try:
            async with owner._provider.synthesize(  # pyright: ignore[reportPrivateUsage]
                self._sentences(),
                voice=owner._voice,  # pyright: ignore[reportPrivateUsage]
                language=owner._language.value,  # pyright: ignore[reportPrivateUsage]
                sample_rate=SAMPLE_RATE,
            ) as audio:
                async for chunk in audio:
                    output_emitter.push(chunk)
        except ProviderError as exc:
            raise to_api_error(exc) from exc
