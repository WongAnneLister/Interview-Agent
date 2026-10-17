# ADR-0001 语音合成选用 Qwen-TTS Realtime（Ryan，1.2 倍语速），保留 CosyVoice 可切换

- 状态：已采纳（2026-10-17）
- 相关：PR-09、PR-09b，[实测结论](../benchmarks/findings.md)

## 背景

PR-09 按计划接入了百炼 CosyVoice 任务协议（cosyvoice-v3-flash），首包低、取消后连接可复用。
但试听四个候选系统音色（longanlang_v3、longshuo_v3、longanwen_v3、longxiaoxia_v3）后，听感偏“AI”。
用户希望用 Qwen3-TTS 的 Ryan（英语）和 Bodega（西班牙口音英语）作为面试官声音。
这两个音色只能通过 Qwen-TTS Realtime 协议（qwen3-tts-flash-realtime）调用。它仍是纯语音合成模型，
放在“识别 → 大模型 → 合成”的级联架构里即可，不需要换成端到端语音模型。

## 决策

- 新增 `bailian_realtime` 适配器（`providers/bailian_tts_realtime.py`），用 commit 模式、客户端按句末标点切句提交。
- 当前配置：`TTS_PROVIDER=bailian_realtime`、`TTS_MODEL=qwen3-tts-flash-realtime`、`TTS_VOICE=Ryan`、
  `TTS_EXTRA_BODY={"speech_rate": 1.2}`（音色本身偏慢，1.2 倍后中文约 7 字/秒）。
- CosyVoice 适配器保留，两者都实现同一个 `TTSProvider` 接口，**切换只改 `.env`**，业务代码不变。
- 继续保持级联架构，不采用端到端语音模型：每一环可单独换厂商、观测和计费，报告需要的“原话证据”直接来自识别文本。

## 代价与风险

| 项目 | Qwen-TTS Realtime | CosyVoice |
|---|---|---|
| 听感 | 更自然，可选口音音色 | 偏“AI” |
| 打断 | 没有取消指令，只能断开连接，下一句要重新建连（可 `warmup()` 预建） | 取消后连接可复用 |
| 首包（2026-10-17） | P50 363 ms / P95 692 ms | 中文约 260–380 ms |
| 稳定性 | 偶发卡住（约 3/14 次，未稳定复现），已加排查日志 | 未发现 |
| 计费 | 1 元/万字符；用量是会话累计值，适配器取增量 | 1 元/万字符 |

## 后续

- 用 `make benchmark` 持续观察卡顿率；如果卡顿影响体验，切回 CosyVoice 或在适配器里加保护。
- 需要印度口音英语时，评估声音复刻（需本人授权）、声音设计，或接 Azure / Google 的 en-IN 语音。
