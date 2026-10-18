# PR-16 面试状态机

纯逻辑，不做 IO，不调用模型，时间由调用方传入。文件：`backend/src/interview/domain/state_machine.py`，测试 `backend/tests/unit/test_state_machine.py`。
src 目标 ≤ 300 行。不改数据库、不加环境变量、不写编排（PR-19）和关键词识别（PR-30）。

已确认：没有题的有题阶段自动跳过；暂停时停表；“等待思考”的标记和超时由状态机管理。

## 阶段

`Phase(StrEnum)`：`opening、self_intro、resume_deep_dive、technical、behavioral、candidate_questions、closing、ended`，取值与 `plan.Stage` 相同，用 `Phase(stage.value)` 转换。
顺序取 `plan.stages` 的列表顺序（`allocate_stages` 已按开场 → 自我介绍 → 深挖 → 技术/行为 → 候选人提问 → 结束排好），最后接 `ended`。
有题阶段是 `resume_deep_dive` 和本场主阶段；题目顺序就是 `plan.questions` 里该阶段题目的列表顺序。

## 时间

- 所有事件都带 `now_ms: int`（调用方的时间轴，单调不减）。`now_ms` 小于上次事件时抛 `ValueError`。
- 状态里有 `clock_ms`（上次事件的 now_ms）和 `active_ms`（累计非暂停时间）。每个事件先更新时钟：未暂停时 `active_ms += now_ms - clock_ms`，然后 `clock_ms = now_ms`。暂停时只更新 `clock_ms`，即停表。
- 截止时间都按 `active_ms` 计算：
  - 阶段截止 = `stage_started_ms + 该阶段 seconds × 1000`
  - 总截止 = 全部阶段 seconds 之和 × 1000（从开始计）
  - 到达截止叫“超时”，超过截止 + `overtime_grace_ms` 叫“硬超时”。超时只影响下一次决策（收尾当前题，不再追问或出新题）；硬超时由 Tick 强制推进，防止候选人一直不说话卡住。
- 阶段提前结束时，剩余时间不顺延给后面的阶段。

```python
@dataclass(frozen=True)
class MachineConfig:
    max_follow_ups: int = 2
    think_timeout_ms: int = 20_000
    overtime_grace_ms: int = 30_000
```

## 状态（Pydantic，可 JSON 序列化）

```python
class InterviewState(BaseModel):
    schema_version: Literal[1] = 1
    phase: Phase
    stage_started_ms: int          # active_ms 时间轴
    active_ms: int
    clock_ms: int
    current_question_id: str | None   # 有题阶段一定有值，其它阶段为 None
    follow_ups: int                # 本题已追问次数，0..max_follow_ups
    asked: list[str]               # 已问过（含跳过）的题目 id，按提问顺序，不重复
    paused: bool
    thinking_since_ms: int | None  # “等待思考”的开始时间（active_ms），None 表示不在等待
    last_directive: Directive | None  # 最近一条有内容的指令，供 repeat / resume 使用
```

`waiting_for_thought` 用属性表示：`thinking_since_ms is not None`。

## 事件（Pydantic，用 `type` 字段区分）

```python
class Tick(BaseModel):     type: Literal["tick"];     now_ms: int
class Decision(BaseModel): type: Literal["decision"]; now_ms: int
                           action: Literal["follow_up", "next_question", "next_stage", "wrap_up"]
                           focus: str = ""        # 追问重点，来自评估器
class Command(BaseModel):  type: Literal["command"];  now_ms: int
                           command: Literal["pause", "resume", "repeat", "skip", "end"]
class Think(BaseModel):    type: Literal["think"];    now_ms: int   # 候选人说“我想一下”
Event = Annotated[Tick | Decision | Command | Think, Field(discriminator="type")]
```

有题阶段的 Decision 来自评估器（评估超时由编排器传 `next_question`）；无题阶段由编排器在候选人说完一轮后传：`follow_up` 表示留在本阶段继续聊，其它值表示进入下一阶段。`wrap_up` 是编排器在预算超限等情况下要求收尾，不来自评估器，所以数据库的 `NextAction` 枚举不加这个值。

## 输出：给前台面试官的指令

```python
class Directive(BaseModel):
    kind: Literal["opening", "ask", "follow_up", "stage_prompt", "converse",
                  "check_in", "repeat", "pause", "resume", "closing"]
    phase: Phase
    question_id: str | None = None
    question_text: str | None = None
    focus: str = ""
    transition_from: Phase | None = None   # 这条指令开启了新阶段时，填上一个阶段，前台据此说过渡语
    reason: Literal["plan", "follow_up_limit", "stage_timeout", "total_timeout",
                    "user_skip", "user_end", "wrap_up", "think_timeout"] | None = None
    repeat_of: str | None = None           # kind=repeat 时填原指令的 kind

class Step(BaseModel):
    state: InterviewState
    directive: Directive | None
```

- `opening`：开场。`stage_prompt`：进入自我介绍或候选人提问阶段时的引导。`ask`：问一道主问题。`follow_up`：按 focus 追问。`converse`：在无题阶段接着聊。`check_in`：思考超时后温和询问。`closing`：收尾致谢。
- `pause` / `resume`：确认暂停和继续。resume 时带上 last_directive 的题目，让前台说“我们继续”并重复题目。
- `repeat`：复制 last_directive 的字段，`kind="repeat"`，`repeat_of` 填原 kind。
- 进入 `ended` 不产生指令，调用方看 `state.phase`。
- 会更新 last_directive 的只有内容类指令：opening、ask、follow_up、stage_prompt、converse、check_in、closing。

## 接口

```python
def start(plan: InterviewPlan, now_ms: int, config: MachineConfig = DEFAULT) -> Step
def step(plan: InterviewPlan, state: InterviewState, event: Event, config: MachineConfig = DEFAULT) -> Step
def stage_remaining_ms(plan: InterviewPlan, state: InterviewState) -> int   # 不小于 0，供 PR-32 显示倒计时
def total_remaining_ms(plan: InterviewPlan, state: InterviewState) -> int
```

纯函数，不修改传入的 state，返回新对象（`model_copy(update=...)`，list 字段要复制）。

`start`：phase=opening，active_ms=0，clock_ms=now_ms，指令为 opening。

## 进入阶段 `enter(next_phase, reason)`

- 设 `phase`，`stage_started_ms = active_ms`，`follow_ups = 0`，清除思考标记。
- 有题阶段：取该阶段第一道不在 asked 里的题，设为 current_question_id 并追加到 asked，指令 `ask`。没有未问的题时，直接进入再下一个阶段（自动跳过空阶段）。
- self_intro / candidate_questions：current_question_id=None，指令 `stage_prompt`。
- closing：指令 `closing`。ended：没有指令，current_question_id=None。
- 指令的 `transition_from` 填进入前的阶段，`reason` 填触发原因。

`next_question_or_stage(reason)`：本阶段已超时时进入下一阶段；否则问本阶段下一道未问的题（follow_ups 归零），没有了就进入下一阶段。next_question、追问已达上限、skip 都调用它。

## 转移表

每个事件依次处理：`ended` 时忽略一切事件（返回原状态和 None）；然后更新时钟；再按下表处理。“忽略”指状态只更新时钟，指令为 None。

**Tick**

| 条件（按顺序检查，命中第一条） | 结果 |
| --- | --- |
| 暂停中 | 忽略 |
| 不在 closing，且总硬超时 | 进入 closing，reason=total_timeout |
| 在 closing，且 closing 阶段硬超时 | 进入 ended |
| 其它阶段硬超时 | 进入下一阶段，reason=stage_timeout |
| 等待思考，且 `active_ms - thinking_since_ms ≥ think_timeout_ms` | 指令 check_in（reason=think_timeout），清除思考标记 |
| 其它 | 忽略 |

**Decision**（暂停中忽略；否则先清除思考标记）

| 条件（按顺序） | 结果 |
| --- | --- |
| 在 closing | 进入 ended |
| 总超时 | 进入 closing，reason=total_timeout |
| action=wrap_up | 进入 closing，reason=wrap_up（排在阶段超时之前：要求收尾时不再多走一个阶段） |
| 本阶段超时 | 进入下一阶段，reason=stage_timeout（收尾当前题） |
| action=next_stage | 进入下一阶段，reason=plan |
| 有题阶段，follow_up，follow_ups < max | 指令 follow_up（focus），follow_ups + 1 |
| 有题阶段，follow_up，follow_ups 已达 max | `next_question_or_stage(follow_up_limit)` |
| 有题阶段，next_question | `next_question_or_stage(plan)` |
| 无题阶段，follow_up | 指令 converse，留在本阶段 |
| 无题阶段，next_question | 进入下一阶段，reason=plan |

**Command**（除 pause 外都先清除思考标记）

| 指令 | 条件 | 结果 |
| --- | --- | --- |
| pause | 在 closing 或已暂停 | 忽略 |
| pause | 其它 | paused=True，清除思考标记，指令 pause |
| resume | 未暂停 | 忽略 |
| resume | 暂停中 | paused=False，指令 resume（带 last_directive 的题目和阶段） |
| end | 在 closing | 进入 ended |
| end | 其它（包括暂停中） | paused=False，进入 closing，reason=user_end |
| repeat / skip | 暂停中 | 忽略 |
| repeat | 其它 | 指令 repeat（复制 last_directive） |
| skip | 在 closing | 忽略 |
| skip | 有题阶段 | `next_question_or_stage(user_skip)` |
| skip | 无题阶段 | 进入下一阶段，reason=user_skip |

**Think**：在 closing 或暂停中时忽略；否则设 `thinking_since_ms = active_ms`，没有指令。

## 不变量（测试要断言）

- `0 ≤ follow_ups ≤ max_follow_ups`；同一道题收到的 follow_up 指令不超过 max_follow_ups 条。
- 阶段只前进、不后退；asked 不重复，且每个 id 都在 plan 里。
- 处在有题阶段时，current_question_id 不为空，且该题的 stage 等于当前阶段；其它阶段为 None。
- 传入的 state 不被修改；`InterviewState.model_validate_json(state.model_dump_json()) == state`。
- 对任意事件序列，末尾追加 `[Command(resume), Tick(+10⁹), Tick(+2×10⁹)]` 后一定到达 ended。

## 测试

- 单元测试覆盖上面每张表的每一行，另外覆盖：
  - start 的开场指令
  - 空阶段被跳过（深挖题全被删）
  - 追问上限
  - 阶段超时后 Decision 收尾、阶段硬超时由 Tick 推进
  - 总超时进入 closing
  - 暂停停表（暂停期间的 Tick 不推进 active_ms，也不触发超时）
  - end 在暂停中也生效
  - repeat 和 resume 带上次的题目
  - 思考超时 check_in，Decision 清除思考标记
  - 时间倒退报错
  - ended 后的事件被忽略
  - 15、20、30 分钟计划都能走完
- hypothesis：
  - 新增开发依赖 `hypothesis`（`uv add --dev hypothesis`，与 pyproject 现有写法一致），`.gitignore` 加 `.hypothesis/`。
  - 策略：从两三份计划里任选一份（20 分钟正常计划、深挖题为空的计划、30 分钟行为面），生成 0–60 个随机事件，时间增量 0–400 000 ms。
  - 每一步都检查全部不变量，并把状态序列化后重新加载再继续，确认结果一致。
  - 设置 `@settings(max_examples=300, deadline=None)`。
