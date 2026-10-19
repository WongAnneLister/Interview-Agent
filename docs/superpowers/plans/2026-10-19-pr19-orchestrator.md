# PR-19 文字面试编排

把状态机、评估器、前台面试官串成一场完整的面试，并且与传输方式无关：文字（PR-20 WebSocket）和语音（PR-29）都调用同一个编排器。

- 文件：
  - 新增：`domain/orchestrator.py`、`domain/history.py`、迁移 `add_interview_state`
  - 测试：`tests/unit/test_orchestrator.py`、`tests/unit/test_history.py`
  - 修改：`db/models.py`（interviews、evaluations 各加一列）
- 不做：
  - WebSocket（PR-20）、语音（PR-29）
  - 报告生成（PR-21，本 PR 只留一个空的结束回调）
  - 断线续面的恢复流程（PR-39，本 PR 只负责把状态存下来）
- src 目标约 450 行。实际写出来约 1080 行，review 时按职责拆成几个模块、分 3 个 commit 提交：
  - `orchestrator_store.py`：数据库读写
  - `orchestrator_events.py`：事件和配置
  - `orchestrator_eval.py`：每轮的评估、预算与降级
  - `orchestrator_speech.py`：前台说话与兜底
  - `orchestrator.py`：主流程

已确认：
- 状态机的状态存在 interviews 表的新列里。
- 历史摘要由代码生成，不调用模型。
- 预算 warn 时只记录，不换型号：前台的 qwen3.8-flash 已经是单价最低的。

## 依赖的已有模块（先读）

- `domain/state_machine.py`：`start`、`step`，事件类 `Tick`、`Decision`、`Command`、`Think`，以及 `Directive`、`InterviewState`、`stage_remaining_ms`、`DEFAULT.max_follow_ups`
- `domain/evaluator.py`：`evaluate`、`EvaluationInput`、`TranscriptTurn`、`EvaluationResult`
- `domain/interviewer.py`：`speak`、`InterviewerInput`、`HistoryTurn`、`TransitionPicker`、`Utterance`
- `domain/plan.py`：`InterviewPlan`、`InterviewRepository`、`PlanRepository`
- `providers/registry.py`：`llm("llm_front", ctx)`、`live_eval_llm(ctx)`，以及 `CallContext`
- `observability/budget.py`：`check(interview_id, user_id=..., sessionmaker=...)`，返回 `BudgetCheck.status`（ok / warn / exceeded）
- `observability/tracing.py` 的 `span`、`open_span`；`observability/prometheus.py` 的 `counter`、`histogram`

## 存储

一个新迁移 `add_interview_state`，照现有迁移的风格手写，`down_revision = "c4d9e2f7a1b3"`，加两列：

- `interviews.state_json`（JSONB，可空）：存 `{"machine": InterviewState.model_dump(mode="json"), "ended_by_user": bool}`，每轮结束后覆盖写入；为空表示面试还没开始。外面包一层是为了放编排器自己的标记，不改状态机的结构。
- `evaluations.detail_json`（JSONB，非空，`server_default='{}'`）：存 `EvaluationResult` 里没有专门列的字段，也就是 missing_points、highlights、rationale、follow_up_focus、override_reason、latency_ms、evidence_dropped、evidence_retried、retries。`evidence_json` 保持原来的含义，只存证据列表。

写入规则：

- 每一轮发言写一条 `Turn`：
  - `seq` 从 1 开始递增，`question_id` 填当前题目，`interrupted` 本 PR 恒为 False（PR-31 实现）。
  - 候选人的 turn：`start_ms` / `end_ms` 用 `on_user_turn` 传入的时间。
  - 面试官的 turn：文字版不知道话什么时候“说完”，`start_ms` 和 `end_ms` 都用触发它的事件时间（`on_user_turn` 时是候选人的 `end_ms`，其它入口是 `now_ms`）。PR-29 接语音时再换成真实的播放时间。
- 每次评估写一条 `Evaluation`（phase=live），字段取自 `EvaluationResult`：
  - `turn_id` 是本题最后一条候选人发言。
  - `model` 写成 `"{provider}/{model}"`，用结果里的实际值（challenges C10）；`prompt_version` 取结果里的值。
  - `evidence_json` 存证据列表，`detail_json` 存其余字段。
- `interviews` 的状态：`start()` 时设 `status=in_progress`、`started_at`；进入 ended 时设 `ended_at`，`status` 按结束原因取值：正常走完是 `completed`，候选人 end 是 `aborted`（枚举的含义见 `db/models.py`）。状态机本身不记录“为什么结束”，由编排器判断：只要收到过 `end` 指令，或者进入 closing 的那条指令 `reason == "user_end"`，就算 aborted，其余都算 completed。这个标记存在 `state_json.ended_by_user` 里，恢复后不会丢。
- 数据库会话只在读写时短暂打开，调用模型期间不占会话（PR-13 起的约定）。

## 接口

```python
class OrchestratorConfig(BaseModel):
    eval_wait_s: float = 2.5           # 等评估器的上限
    machine: MachineConfig = DEFAULT   # 状态机配置

class InterviewOrchestrator:
    def __init__(self, *, interview_id, user_id, sessionmaker, providers: ProviderRegistry,
                 config: OrchestratorConfig = OrchestratorConfig(), on_ended: Callable[[uuid.UUID], Awaitable[None]] | None = None)

    @classmethod
    async def load(cls, ...) -> InterviewOrchestrator   # 读 interview、plan、已有 turns；state_json 为空时视为新面试

    def start(self, now_ms: int) -> AsyncIterator[OrchestratorEvent]
    def on_user_turn(self, text: str, start_ms: int, end_ms: int) -> AsyncIterator[OrchestratorEvent]
    def on_command(self, command: Literal["pause", "resume", "repeat", "skip", "end"], now_ms: int) -> AsyncIterator[OrchestratorEvent]
    def on_think(self, now_ms: int) -> AsyncIterator[OrchestratorEvent]   # 候选人说“我想一下”（PR-30 调用）
    def on_tick(self, now_ms: int) -> AsyncIterator[OrchestratorEvent]    # 时间推进（超时、思考超时）

    async def aclose(self) -> None   # 等待未完成的评估任务落库后再返回（最多等 config.eval_wait_s × 4）
```

- 所有入口都是异步生成器，调用方 `async for` 消费事件。调用方中途不想要了，要调用生成器的 `aclose()`。
- 同一时刻只处理一个入口，用 `asyncio.Lock` 串行化。语音层可能在面试官说话时收到新的事件，这时排队等待，不并发执行。锁在生成器的 `finally` 里释放，消费方中途 `aclose()` 也不会一直占着锁；这一点要有测试。
- 时间全部由调用方传入（毫秒，单调不减），编排器不读系统时间；测量耗时用 `time.perf_counter()` 不算违反这一点。
- `on_ended` 在进入 ended、`interviews` 的状态写好之后调用一次，PR-21 在这里接报告生成。回调出错只记日志，不影响面试结束。

### 事件（Pydantic，用 `type` 字段区分，PR-20 导出给前端）

```python
class InterviewerDelta(BaseModel):    type: Literal["interviewer_delta"];    text: str       # 本 PR 不细分 token，每句吐一次，text=整句
class InterviewerSentence(BaseModel): type: Literal["interviewer_sentence"]; text: str; transition: bool = False
class InterviewerDone(BaseModel):     type: Literal["interviewer_done"];     turn_id: uuid.UUID; text: str
class StateEvent(BaseModel):          type: Literal["state"];  value: Literal["listening", "thinking", "speaking", "paused"]
class StageEvent(BaseModel):          type: Literal["stage"];  phase: Phase; remaining_s: int; total_remaining_s: int
class EndedEvent(BaseModel):          type: Literal["ended"]
OrchestratorEvent = Annotated[InterviewerDelta | InterviewerSentence | InterviewerDone | StateEvent | StageEvent | EndedEvent, Field(discriminator="type")]
```

- `stage` 的剩余时间用 `state_machine.stage_remaining_ms()`、`total_remaining_ms()` 换算成秒。
- 暂停后发 `state=paused`，恢复后回到 listening。

`interviewer_delta` 先和 `interviewer_sentence` 一样逐句发出，给 PR-20/32 留出更细粒度流式的位置；不必为了它去改 `Utterance`。

## 时序：on_user_turn

1. 在锁内处理。暂停中或已经 ended 时，记一条 warning 日志，只把这一轮写成 turn，不推进状态。
2. 落库：写一条 candidate turn，并把它加进本题的问答缓存。同时启动预算检查（见第 8 步），它和评估并行，不额外拖慢这一轮。
3. 发出 `state=thinking`。
4. 启动评估（有题阶段才评估）：
   - 用 `asyncio.create_task(evaluate(...))` 启动，模型取 `providers.live_eval_llm(CallContext(user_id, interview_id, turn_id=候选人 turn, usage_role=EVAL))`。
   - 输入是当前题、能力项、本题全部问答、`state.follow_ups`、`stage_remaining_ms`。
5. **先说过渡语**：用 `TransitionPicker.pick()` 选一句，发出 `interviewer_sentence(transition=True)`。过渡语不调用模型，也不写 turn；它会拼进下一条面试官 turn 的文字开头，这样转写里能看到。
6. 用 `asyncio.wait_for(asyncio.shield(task), timeout=config.eval_wait_s)` 等评估结果：
   - **按时返回**：`Decision(action=result.next_action, focus=result.follow_up_focus)` 推进状态机。
   - **超时**：`Decision(action="next_question")` 推进状态机，也就是按计划顺序问下一题。span 记 `orchestrator.eval_degraded=True`，计数器 `orchestrator_eval_degraded{reason="timeout"}` 加一。评估任务**不取消**，继续在后台运行，完成后照样落库。
   - **评估报错**（ProviderError、StructuredOutputError）：按超时处理，`reason="error"`，日志只记错误类型。
   - **无题阶段**（自我介绍、候选人提问）：不评估，直接发 `Decision(action="follow_up")` 留在本阶段。阶段超时由状态机处理。
   - 状态机的 `Decision` 时间用 `end_ms`，也就是候选人说完的时刻。
7. **评估结果落库**：第 4 步启动评估时，同时用 `asyncio.create_task(self._store_evaluation(task))` 启动一个落库任务，放进 `self._pending` 集合，完成后从集合里移除。落库任务等评估完成，再另开一个短会话写 Evaluation，不管第 6 步是否超时都写；评估报错就不写，只记日志。晚到的评估只落库，不再影响已经发生的提问。`aclose()` 等这个集合清空后再返回。不要用 `add_done_callback` 写库：回调是同步的，不能在里面 await。
8. **预算检查**：在第 2 步启动 `budget.check`，结果必须在第 6 步把 Decision 交给状态机**之前**拿到。写在第 8 步只是为了把规则集中说明。
   - `warn`：记 warning 日志，span 打 `budget.status=warn`，不换型号。
   - `exceeded`：不管评估结果是什么，第 6 步交给状态机的 Decision 都换成 `wrap_up`，进入收尾。
   - check 本身出错时（比如数据库不通）记日志，按 ok 继续，不能因为预算查询失败中断面试。
   - `start`、`on_command`、`on_tick` 不检查预算：它们不会产生新的评估调用，前台的花费很小。
9. 状态机给出的指令交给 `_speak(directive)`，见下一节。
10. 每轮结束后在一个短会话里写入 `state_json`。阶段变化时发 `stage` 事件；进入 ended 时发 `ended` 并调用 `on_ended`。

`start`、`on_command`、`on_think`、`on_tick` 都复用第 9–10 步：调用状态机，把有指令的结果交给 `_speak`，最后存状态。`on_think` 和大部分 `on_tick` 不产生指令，只存状态。

## _speak(directive)

- 发出 `state=speaking`。
- 用 `speak(providers.llm("llm_front", ctx), InterviewerInput(directive, style, language, history=build_history(...)), interview_id=...)` 生成，逐句发出 `interviewer_delta` 和 `interviewer_sentence`。
- 生成完写一条 interviewer turn（文字是“过渡语 + 生成的整段”），发出 `interviewer_done`，然后发出 `state=listening`。
- 前台模型报错（ProviderError）时，改用一句固定兜底台词，按语言给出：`"不好意思，我们继续。"` 或 `"Sorry, let's continue."`，加上题目原文（有的话）。照常写 turn，日志记错误类型，计数器 `orchestrator_front_fallback` 加一。面试不能因为前台出错而中断。
- `pause` 和 `resume` 指令也走 `_speak`。

## 对话历史压缩：domain/history.py（纯函数）

```python
def build_history(turns: Sequence[TurnRecord], plan: InterviewPlan, current_phase: Phase, *, language: Language) -> list[HistoryTurn]
```

- 当前阶段的问答原样保留，按时间顺序，每条截断到 `MAX_TURN_CHARS = 400`。
- 更早的阶段每个压成一条面试官的摘要。摘要由代码生成，不调用模型：
  - 中文：“（已完成：简历深挖阶段，问了 2 道题：深挖 Ledger 项目、深挖 Northwind Labs）”
  - 英文用对应的英文句子。
  - 题目名取 `Question.text` 的前 20 个字。
- 结果最多 `interviewer.MAX_HISTORY_TURNS` 条，超出时丢掉当前阶段最早的问答，摘要保留。
- `TurnRecord` 是从 Turn 表读出来的轻量结构（role、text、question_id、phase）。turns 表没有 phase 列，phase 在写 turn 时由编排器记在内存里。从数据库恢复（`load()`）时用 `infer_phases(turns, plan)` 推出来：
  - 有 `question_id` 的 turn 取那道题的 stage；
  - 没有的沿用它前面最近一条 turn 的 phase，第一条之前算 opening；
  - 推出的 phase 只能前进不能后退，遇到“倒退”时沿用前一条的。
  这是尽力而为的规则，足够用来压缩历史；PR-39 断线续面如果需要更准，再给 turns 表加列。

## 可观测性

- 一场面试是一个 trace：`start()` 开一个根 span `interview.session`，用 `open_span` 实现，在 `aclose()` 或进入 ended 时结束。
- 每轮是它下面的子 span `interview.turn`，属性：
  - `turn.kind`（user_turn / command / tick / think / start）
  - `directive.kind`、`state.phase`、`orchestrator.eval_degraded`、`orchestrator.eval_wait_ms`、`budget.status`
- Prometheus：
  - `orchestrator_eval_degraded`（计数，标签 reason=timeout / error）
  - `orchestrator_eval_wait_seconds`（直方图，桶 0.5、1、1.5、2、2.5、3、5）
  - `orchestrator_front_fallback`（计数）
- 日志只记 interview_id、轮次、阶段、指令类型、耗时、是否降级，不记候选人和面试官的话。

## 测试（全部用假厂商、SQLite，不调用真实厂商）

- 照 `test_interviews_api.py` 的 SQLite 建表方式，并复用 `sqlite_helpers.py`。经注册表调用模型的测试都要 mock `usage.record_usage`（C12）；`budget.check` 用 monkeypatch 返回指定状态。
- 用例：
  - 完整一场：FakeLLM 给前台固定台词，评估器的 FakeLLM 先给 follow_up 再给 next_question。从开场一直跑到 ended，状态机的每个阶段都经过，turn 的 seq 连续，Evaluation 有记录，`on_ended` 被调用一次。
  - 评估超时降级：评估器 FakeLLM 设 `first_token_delay_s` 大于等待上限，状态机收到 next_question，降级计数器加一，评估结果晚到后仍然落库（等 `aclose()` 返回后检查）。
  - 评估报错降级：评估器抛 ProviderTimeout，按 next_question 推进，计数器的 reason 是 error。
  - 追问上限：连续 3 次 follow_up，第 3 次被状态机或评估器改成换题。
  - 阶段超时：`on_tick` 推到阶段截止加宽限之后，进入下一阶段并发出 stage 事件。
  - 各个用户指令：pause（暂停中提交回答不推进状态）、resume、repeat、skip、end。
  - 过渡语：每次 on_user_turn 的第一条句子事件是 transition=True，并且会出现在下一条面试官 turn 的文字里。
  - 前台出错兜底：前台 FakeLLM 抛 ProviderUnavailable，发出兜底台词，面试继续。
  - 预算：warn 时不改指令；exceeded 时进入 closing。
  - 状态持久化：每轮结束后 `state_json` 可以还原成和内存里一样的 InterviewState；用 `load()` 读出来能接着跑。
  - 历史压缩（`test_history.py`）：当前阶段原样保留、更早阶段压成摘要、超长截断、条数上限；`infer_phases` 覆盖有题、无题、开场、“倒退”几种情况。
  - 串行：同时调用两个入口时，第二个等第一个结束后才执行；消费方只取了第一个事件就 `aclose()` 生成器，锁被释放，下一个入口能正常执行。
  - 面试状态：start 后是 in_progress 并带 started_at；正常走完是 completed，候选人 end 是 aborted，都带 ended_at。
  - 评估记录：`model` 是实际作答的厂商和型号，`detail_json` 里有 rationale 等字段。
  - 日志不含候选人和面试官的原话。
- 提交前把 `.env` 临时挪走跑一次单元测试（C12）。

## 实时评估的备用（challenges C1 遗留）

本 PR 不改备用配置，只在编排器里把评估报错当作降级处理（见“时序”第 6 步），这样 DeepSeek 不可用时面试仍能按计划进行。是否给实时评估另配百炼 max 做备用，等 PR-19 合并后看降级率再决定。
