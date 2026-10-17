"""合成实测：20 句面试官台词（中英都有），连接预热后测首包耗时 P50 / P95，保存音频供人工试听。

文本按大模型的节奏分段送入（每 50 ms 6 个字），与真实链路一致；每句之前先 warmup()，
已有可用连接时不会新建。合成失败（如超时）记为失败并继续，报告里给出失败数。
音频写到 tools/benchmark/out/<时间>/（不提交）。

单独运行：PYTHONPATH=tools uv run python -m benchmark.tts
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

from benchmark.common import OUT_DIR, like_llm, now, provider_settings, region, write_wav
from benchmark.metrics import mean_stdev, ms, percentile, table
from interview.providers.base import ProviderError
from interview.providers.registry import default_factories

SAMPLE_RATE = 24000
LINES = [
    "你好，欢迎参加今天的面试。我们先从自我介绍开始吧。",
    "好的，谢谢。能具体说说你在这个项目里负责哪一部分吗？",
    "这个方案上线之后，效果是怎么衡量的？",
    "如果流量突然涨了十倍，你会先从哪里排查？",
    "嗯，明白了。那消息重复消费的问题你们是怎么处理的？",
    "你刚才提到了缓存，能讲讲缓存和数据库的一致性是怎么保证的吗？",
    "这个设计里你觉得最大的风险是什么？",
    "好，我们换个话题，聊聊你和团队协作的经历。",
    "有没有遇到过和同事意见不一致的时候？最后是怎么解决的？",
    "时间差不多了，你有什么想问我的吗？",
    "Hi, thanks for joining today. Could you briefly introduce yourself?",
    "Great. What was the hardest technical problem you solved last year?",
    "How did you measure whether the change actually worked?",
    "Can you walk me through the data model you designed?",
    "What would you do differently if you built it again?",
    "Okay. How do you usually handle disagreements in code review?",
    "Let's switch gears. Tell me about a time you missed a deadline.",
    "That makes sense. How did you communicate the delay to stakeholders?",
    "Do you have any questions for me?",
    "Thanks for your time today. We'll be in touch soon.",
]


@dataclass(frozen=True)
class Line:
    index: int
    text: str
    first_latency_ms: float | None
    audio_seconds: float
    characters: int | None
    error: str | None


@dataclass(frozen=True)
class TtsResult:
    provider: str
    model: str
    voice: str
    url: str
    extra: dict[str, object]
    out_dir: Path
    lines: list[Line]


async def run(out_dir: Path | None = None) -> TtsResult:
    settings = provider_settings()
    role = settings.tts
    tts = default_factories(settings).tts[role.provider](role)
    folder = out_dir or OUT_DIR / now().strftime("%Y-%m-%d_%H%M")
    lines: list[Line] = []
    try:
        await tts.warmup()
        for index, text in enumerate(LINES, start=1):
            await tts.warmup()  # 已有可用连接时不新建；上一句失败断开后在这里补建
            try:
                async with tts.synthesize(like_llm(text), sample_rate=SAMPLE_RATE) as stream:
                    pcm = b"".join([frame async for frame in stream])
            except ProviderError as exc:
                lines.append(Line(index, text, None, 0.0, None, type(exc).__name__))
                print(f"  [tts] 第 {index} 句失败：{exc}", file=sys.stderr)
                continue
            write_wav(folder / f"tts_{index:02d}.wav", pcm, SAMPLE_RATE)
            usage = stream.usage
            lines.append(
                Line(
                    index,
                    text,
                    stream.first_latency_ms,
                    len(pcm) / 2 / SAMPLE_RATE,
                    usage.characters if usage else None,
                    None,
                )
            )
            print(f"  [tts] 第 {index} 句完成", file=sys.stderr)
    finally:
        await tts.aclose()
    return TtsResult(
        provider=role.provider,
        model=role.model,
        voice=getattr(role, "voice", ""),
        url=role.base_url,
        extra=dict(role.extra_body),
        out_dir=folder,
        lines=lines,
    )


def render(result: TtsResult) -> str:
    ok = [line for line in result.lines if line.error is None]
    firsts = [line.first_latency_ms for line in ok if line.first_latency_ms is not None]
    zh = [
        line.first_latency_ms
        for line in ok
        if line.index <= 10 and line.first_latency_ms is not None  # 前 10 句是中文
    ]
    en = [
        line.first_latency_ms
        for line in ok
        if line.index > 10 and line.first_latency_ms is not None
    ]
    mean, stdev = mean_stdev(firsts)
    summary = table(
        ["范围", "句数", "首包 P50", "首包 P95", "均值 ± 标准差"],
        [
            [
                "全部",
                len(firsts),
                ms(percentile(firsts, 50)),
                ms(percentile(firsts, 95)),
                f"{ms(mean)} ± {ms(stdev)}",
            ],
            ["中文", len(zh), ms(percentile(zh, 50)), ms(percentile(zh, 95)), "-"],
            ["英文", len(en), ms(percentile(en, 50)), ms(percentile(en, 95)), "-"],
        ],
    )
    detail = table(
        ["#", "台词", "首包", "音频", "计费字符", "结果"],
        [
            [
                line.index,
                line.text,
                ms(line.first_latency_ms) if line.first_latency_ms is not None else "-",
                f"{line.audio_seconds:.1f} 秒",
                line.characters if line.characters is not None else "-",
                line.error or "成功",
            ]
            for line in result.lines
        ],
    )
    failures = len(result.lines) - len(ok)
    return "\n\n".join(
        [
            "## 语音合成",
            f"`{result.provider}` / `{result.model}`，音色 `{result.voice}`，"
            f"参数 `{result.extra or {}}`，地域 {region(result.url)}，输出 {SAMPLE_RATE} Hz PCM。"
            "连接已预热；文本按大模型节奏分段送入；首包 = 发出第一段文本到收到第一块音频。"
            f"失败 {failures}/{len(result.lines)} 句。"
            f"音频保存在 `{result.out_dir.name}/`（不提交）。",
            summary,
            detail,
        ]
    )


def main() -> int:
    print(render(asyncio.run(run())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
