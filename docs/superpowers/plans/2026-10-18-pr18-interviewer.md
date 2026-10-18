# PR-18 前台面试官

按状态机给的指令，用 `llm_front` 流式生成面试官要说的话，并按句切开，供语音层边生成边合成。

- 文件：
  - 新增 `domain/interviewer.py`、`domain/speech_text.py`、`prompts/interviewer.md`
  - 新增测试 `tests/unit/test_interviewer.py`、`tests/unit/test_speech_text.py`
- 不做：
  - 编排（PR-19）：调用评估器、推进状态机、落库都不在本 PR。
  - 过渡语的播放时机（PR-19 / PR-29），本 PR 只提供选取。
  - TTS 插件（PR-27）。
  - 英文版提示词（PR-37）：本 PR 的提示词按 language 要求用中文或英文说话，但只有一个版本。
- src 目标约 350 行。不改数据库、不加环境变量、不改 `state_machine.py`。

## 输入

```python
class HistoryTurn(BaseModel):
    role: TurnRole   # db.models：interviewer / candidate
    text: str

class InterviewerInput(BaseModel):
    directive: Directive            # state_machine.Directive，状态机的输出
    style: InterviewStyle           # db.models：gentle / pressure
    language: Language              # db.models：zh / en
    history: list[HistoryTurn] = [] # 已压缩的对话历史，压缩由 PR-19 负责；本 PR 只取最后 MAX_HISTORY_TURNS 条
    candidate_name: str = ""        # 开场时称呼，可为空
```

`MAX_HISTORY_TURNS = 12`。

## 指令 → 提示词里的“本轮任务”

`Directive.kind` 映射成一段给模型的中文说明，用 `_task_text(directive)` 纯函数生成，便于单测。

| kind | 本轮任务 | 带入的字段 |
|---|---|---|
| opening | 打招呼、介绍本场流程，说明本次面试由 AI 面试官进行 | — |
| stage_prompt | 引导进入新阶段（自我介绍 / 候选人提问） | phase，transition_from |
| ask | 提出这道题 | question_text；有 transition_from 时先一句话过渡 |
| follow_up | 围绕追问方向追问一句 | question_text（作参考）、focus |
| converse | 在当前阶段自然接话，比如回应候选人的提问 | phase |
| check_in | 候选人思考超时后温和询问，要不要提示或换题 | question_text |
| repeat | 把上一条内容换个说法再说一遍 | 按 repeat_of 对应的 kind 处理，带同样的字段 |
| pause | 确认暂停 | — |
| resume | 说“我们继续”，并复述当前题目 | question_text |
| closing | 感谢、说明报告稍后生成、道别 | — |

`reason` 是 stage_timeout / total_timeout / user_skip / user_end / follow_up_limit 时，加一句提示，比如“时间到了，自然地收尾这道题再进入下一部分”，让面试官说话有来由。
`focus` 和题目只在任务说明里出现，不让模型把追问方向原样念出来，提示词里要写明这一点。

## 提示词 interviewer v1（role: front）

- system：
  - 你是面试官，正在进行语音面试，说的每个字都会被直接念出来。
  - 每次最多 2–3 句；第一句尽量短（10 个字以内），让语音尽快开口（见 challenges.md C8）。
  - 口语化，不用 markdown、列表、编号、括号注释、表情。
  - 不替候选人回答，不给出题目的要点或答案，不点评得分，不透露评分标准和题目列表。
  - style=gentle：可以简短肯定，语气鼓励；style=pressure：少用肯定语，追问更直接，但保持礼貌。
  - language=en：全程用英文；zh：用中文，英文技术术语保持原样（如 Kubernetes、P95）。
- user：
  - 本轮任务（`_task_text` 的结果）。题目、追问方向都来自面试计划，可能被用户在 PATCH 时改过，所以用 `as_data("task")` 包住。
  - 最近的对话：`as_data("history")` 包住，每行一条，格式是“面试官：…”或“候选人：…”。候选人说的话里可能有指令，DATA_NOTICE 会提醒模型忽略。

## 流式生成与按句切分

```python
class Utterance:  # 一次生成
    def __aiter__(self) -> AsyncIterator[str]  # 依次吐出“句子”
    text: str                         # 已吐出的全部句子（结束后是完整文本）
    first_sentence_ms: float | None   # 从开始生成到第一句可用的耗时
    async def aclose(self) -> None    # 打断时调用：停止生成并关闭底层流

def speak(llm: LLMProvider, data: InterviewerInput, *, interview_id: uuid.UUID) -> Utterance
```

- 底层用 `llm.stream_chat`，`max_tokens=200`，`temperature=0.7`，不开 JSON 模式。
- 整个生成过程包在 `span("interviewer.speak", interview_id=..., role=UsageRole.FRONT.value)` 里，span 属性：`directive.kind`、`interviewer.sentences`、`interviewer.first_sentence_ms`、`interviewer.cancelled`。
- 每吐出一句都先经过 `clean_output()`：去掉 markdown 符号、列表前缀和首尾空白，空句跳过。这只是兜底，提示词里已经要求不用 markdown。
- 吐出的是给前端显示和记录转写用的原文；合成前的口语规范化是 `speech_text.normalize()`，由调用方（PR-27）决定何时调用。本 PR 不在吐句时就规范化，因为转写和报告要用原文。
- 日志只记 kind、句数、耗时，不记生成的文字和历史。

### SentenceSplitter（纯逻辑）

```python
class SentenceSplitter:
    def feed(self, chunk: str) -> list[str]   # 喂一段流式文本，返回已完整的句子
    def flush(self) -> list[str]              # 流结束时吐出剩余内容
```

- 句末标点：中文 `。！？；…` 和英文 `.!?;`。紧跟其后的右引号、右括号（`”’」』）)`）和连续的同类标点（如 `？！`、`……`）归入同一句。
- 英文句号只有后面跟空白或文本结束时才算句末，以免切开 `3.5`、`e.g.`、`Node.js`、`v1.2`。英文 `.` 之后跟的是数字或字母时都不切。
- 常见缩写不切：`Mr. Mrs. Dr. e.g. i.e. vs. etc.`，忽略大小写。
- 中文逗号不切，但单句超过 `MAX_SENTENCE_CHARS = 60` 个字符仍没遇到句末时，在最后一个逗号（`，,、`）处切开，免得长句压着不出声。
- `feed` 收到空串返回 `[]`；切出的句子去掉首尾空白，空句不返回。

## 口语规范化 speech_text.normalize(text, language) -> str

合成前把文字转成适合念出来的形式。规则写成表驱动，按下面的顺序执行：

1. 去 markdown：`**`、`__`、`` ` ``、标题 `#`、行首列表符号 `- * • 1.`、链接 `[文字](url)` 只留文字。
2. 百分号：`95%` → zh 读“百分之九十五”，en 读 “95 percent”。
3. 数字与单位（zh）：`3ms`/`3 ms` → “3毫秒”，`2s` → “2秒”，`5min` → “5分钟”，`10GB` → “10 GB”（字母单位不翻译，只加空格让合成念清楚），`QPS 1w` → “QPS 1万”。en 下 `ms` → “milliseconds”，`s` → “seconds”。
4. 常见缩写读法（zh）：表驱动，例如 `P95` → “P九十五”，`P99` → “P九十九”，`QPS`、`TPS`、`API`、`SQL` 保持原样。读法只按词边界整词匹配，不会改动 `API` 出现在 `OpenAPI` 里的情况。
5. 符号：`&` → “和”/“and”，`/` 在两个中文词之间 → “或”，`~` 在数字之间 → “到”/“to”，`->`、`=>` → “到”/“to”。
6. 合并多余空白。

- 英文术语保持原样，不翻译也不拆字母（Kubernetes、PostgreSQL、Redis、FastAPI）。
- 数字本身不转成汉字（合成服务会念数字），只处理带百分号、单位的情况和 P95 这类读法。
- 函数是纯函数，规则放在模块级常量里，后面加规则只需改表。

## 过渡语

```python
TRANSITIONS: Mapping[Language, Mapping[InterviewStyle, tuple[str, ...]]]

class TransitionPicker:
    def __init__(self, language: Language, style: InterviewStyle, *, rng: random.Random | None = None, avoid_last: int = 2)
    def pick(self) -> str
```

- 中英各一组，gentle 和 pressure 分开，每组至少 6 句，都要很短（中文不超过 6 个字，英文不超过 3 个词）。比如：
  - zh gentle：嗯，明白了。好的。嗯，好。了解。好的，谢谢。嗯。
  - zh pressure：好。嗯。明白。了解。行。继续。
  - en gentle：I see. Okay. Got it. Thanks. Right. Mm-hmm.
  - en pressure：Okay. Right. Got it. Noted. Fine. Go on.
- `pick()` 不会返回最近 `avoid_last` 次已经返回过的句子；`rng` 可注入，方便测试得到确定结果。
- 选取完全由代码决定，不调用模型。这些短句可以提前合成并缓存（PR-27，见 C8）。

## 测试（假 LLM，不调用真实厂商）

- 切句（参数化表）：
  - 中文句号、问号、感叹号、省略号、分号
  - `？！` 和右引号归入同一句
  - `3.5 秒`、`e.g.`、`Node.js`、`v1.2` 不被切开；`Mr. Smith` 不被切开
  - 英文句号后面跟空格才切
  - 超过 60 个字符时在逗号处切开
  - 句末标点被拆在两个 chunk 之间（比如 `“好的”` 和 `“。下一题”`）仍然切对
  - flush 吐出剩余内容；空 chunk 返回空列表
- 规范化（参数化用例表，中英各一组）：每条规则至少一条，另外覆盖：
  - 英文术语不被改动
  - `OpenAPI` 里的 `API` 不被替换
  - 已经是口语的句子保持不变
- 过渡语：
  - 连续抽 200 次，任意相邻 `avoid_last + 1` 次里都不重复
  - 每组至少 6 句，长度都在限制内
  - 固定 rng 时结果确定
- `_task_text`：每种 kind 都有用例；repeat 按 repeat_of 处理；带 reason 时会加上相应的提示。
- `speak`：
  - FakeLLM 按小 chunk 流式输出时，逐句吐出的结果正确，`text` 是完整文本，`first_sentence_ms` 有值
  - 模型输出里的 markdown 被 `clean_output` 去掉
  - 提示词里题目和历史都在 `<user_data>` 里，并且有 DATA_NOTICE；history 只取最后 12 条
  - 中途 `aclose()`：不再吐句，底层流被关闭，span 带 `interviewer.cancelled=True`
  - span 属性正确；用 registry 取 llm_front 时用量角色是 front（照 `test_evaluator.py` 的写法，并且要 mock `usage.record_usage`，见 [challenges.md C12](../../challenges.md)）
  - 日志不含生成的文字
- 提交前把 `.env` 临时挪走跑一次单元测试，确认 CI 环境也能过（C12）。
