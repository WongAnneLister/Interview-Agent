# PR-24 前端文字面试页：执行计划

> 给执行代码的 agent：
> - 照这份计划实现，**不要 commit**，**不改后端**。
> - 完成后跑 `make check` 和 `make web-build`，两项都要通过。
> - 结尾按“交付时汇报”一节列出与计划不一致的地方。

验收：在浏览器里用文字完整做完一场面试，结束后跳转到报告页。报告页这次只是占位，正式页面在 PR-25 做。

## 先读

- `CLAUDE.md`
- `backend/src/interview/api/routes/text_ws.py`：模块 docstring 写了**完整协议**，包括消息格式、关闭码、心跳和断线规则，必须全部读完。
- `backend/src/interview/domain/orchestrator_events.py`：服务端事件的定义。
- `backend/src/interview/domain/orchestrator.py`，重点看以下几个函数：
  - `_speak_events`：一次面试官发言的事件顺序是 `state=speaking` → 若干 `interviewer_delta` / `interviewer_sentence` → `interviewer_done` → `state=listening` 或 `paused`；
  - `on_user_turn`：收到回答后先发 `state=thinking` 和一句**只有 `interviewer_sentence(transition=true)`、没有 delta** 的过渡语，再进入面试官发言；
  - `_respond`：**只在阶段变化时**发 `stage` 事件；
  - `snapshot`：重新连接时只推送阶段和状态。
- `backend/src/interview/domain/state_machine.py` 的 `_command`，看清各命令在什么状态下是空操作：
  - 暂停中，`repeat` 和 `skip` 无效；
  - 收尾阶段，`skip` 和 `pause` 无效；
  - 暂停中发来的回答会被记录但忽略（见 `on_user_turn` 里的 `paused` 分支）。
- `web/` 现有代码：
  - `lib/api/schema.d.ts` 里的 `TextServerMessage`、`TextClientMessage`、`Phase`、`ErrorEvent`；
  - `lib/api/errors.ts` 的 `ApiError`，`components/ErrorNotice.tsx`，`lib/styles.ts`，`lib/labels.ts`；
  - `app/interviews/[id]/text/page.tsx` 现在是占位页，这次替换它。

## 协议要点（从后端代码整理，以代码为准）

- **地址**：`ws(s)://<API 主机>/ws/interviews/{id}/text`。由 `NEXT_PUBLIC_API_BASE_URL` 推出：把开头的 `http` 换成 `ws`。这个转换写成纯函数，放进 `lib/api/client.ts` 旁边，并写测试。
- **客户端发送**：
  - `{"type":"user_text","text":...}`：1–4000 字，不能全是空白；
  - `{"type":"command","command":"pause"|"resume"|"repeat"|"skip"|"end"}`；
  - `{"type":"ping"}`：每 20 秒一次。
- **服务端发送**：
  - `interviewer_delta`：每次是**一整句**，不是逐字；
  - `interviewer_sentence`：其中 `transition=true` 的是过渡语，它**没有对应的 delta**；
  - `interviewer_done`：`text` 是这次发言的完整文本（包括过渡语），`turn_id` 唯一；
  - `state`：取值 `listening` / `thinking` / `speaking` / `paused`；
  - `stage`：字段 `phase`、`remaining_s`、`total_remaining_s`；
  - `ended`、`pong`；
  - `error`：字段 `code`、`message`、`trace_id`。
- **服务端处理顺序**：消息按顺序处理。面试官说话期间发来的命令和回答会排队，等这次发言结束才处理。队列最多 8 条，超出时返回 `error`，code 为 `too_many_messages`。
- **计时**：服务端掌握时间，前端不发 tick；暂停期间和断线期间都不计时。`stage` 只在阶段变化时推送，所以前端要在两次 `stage` 之间**自己倒计时**，规则见下文。
- **关闭码**：

  | 关闭码 | 含义 | 前端处理 |
  | --- | --- | --- |
  | 1000（收到 `ended` 之后） | 面试正常结束 | 跳转报告页 |
  | 4410 | 面试已结束 | 跳转报告页 |
  | 4404 | 面试不存在 | 显示错误，不重连 |
  | 4409 | 这场面试已在其他窗口打开 | 见“重连” |
  | 4408 | 空闲超时 | 自动重连 |
  | 1011、1006、其他 | 服务端出错或网络断开 | 自动重连 |

  被拒绝的连接会先收到一条 `error` 消息，然后才关闭；这条消息里的 `message` 和 `trace_id` 要显示出来。

## 必须处理的坑

1. **React StrictMode 下开发环境会连两次**。Next.js App Router 默认开启 StrictMode：effect 先执行，立刻清理，再执行一次。这会带来两个问题：
   - 第一个连接刚连上就被关掉。对一场新面试来说，这会**打断开场白**（第一个连接已经触发了 `start`）。
   - 服务端清理旧连接要花时间：要写库，还要等后台评估，最长约 10 秒。在这段时间里，第二个连接会收到 4409。

   做法：在 effect 里用 `setTimeout(connect, 0)` 延后建立连接，cleanup 时 `clearTimeout`。这样 StrictMode 的立即清理会在连接建立前就把它取消。**不要关闭 StrictMode。**
2. **中文输入法**：按 Enter 发送、Shift+Enter 换行；但输入法正在组字时（`event.nativeEvent.isComposing`，或 `keyCode === 229`）按 Enter 不能发送。
3. **重连后也会遇到 4409**：断线后服务端还在清理旧连接，这时立刻重连会收到 4409。规则是：
   - **自动重连过程中**收到 4409，按退避间隔继续重试；
   - **页面第一次连接**就收到 4409，说明面试真的在别的窗口打开着。这时显示“这场面试已在其他窗口打开”和一个“重新连接”按钮，不自动重试。
4. **刷新页面后历史对话会丢**：后端没有读取历史对话的接口，重连时也只推送阶段和状态。
   - 同一个页面里断线重连，保留内存中的对话即可；
   - 刷新页面或重新打开时，对话区显示一条提示：“已恢复面试，之前的对话不在此显示；可点‘再说一遍题目’重听当前问题”。

   不要为此改后端。完整续面是 PR-39 的工作。
5. **后台标签页的定时器会被浏览器节流**，心跳可能被拖到超过 60 秒，导致服务端以 4408 断开。这种情况交给自动重连处理即可，不需要额外设计，但在汇报的“风险”里写上。

## 范围

要做：

1. 文字面试页 `/interviews/[id]/text`，替换现在的占位页。
2. 纯函数的会话状态 reducer 和重连策略，用 Vitest 测试。
3. 报告页占位 `/interviews/[id]/report`：显示“面试已结束，报告正在生成；报告页将在下一个版本提供”，并提供返回 `/new` 的链接。PR-25 会替换它。

不做：

- 语音；
- 报告页的正式内容；
- 加载历史对话（需要后端接口）；
- 后端改动；
- 新依赖：不加状态库或 WebSocket 库，直接用浏览器原生的 `WebSocket`。

## 1. 会话状态：`web/src/lib/interview/session.ts`（纯函数，不依赖 React）

```ts
type Message =
  | { id: string; role: "interviewer"; text: string; done: boolean }
  | { id: string; role: "candidate"; text: string };

type Connection =
  | "connecting" | "open" | "reconnecting" | "ended"
  | "failed"; // 4404、首次连接即 4409、重试次数用完

type SessionState = {
  connection: Connection;
  activity: "listening" | "thinking" | "speaking" | "paused" | null; // 来自 state 事件
  phase: Phase | null;
  stageRemainingS: number | null;
  totalRemainingS: number | null;
  messages: Message[];
  resumed: boolean;          // 连上后收到的第一条是 stage 事件（而不是面试官说话），说明是续面
  error: ApiError | null;    // 最近一次 error 事件，转成 ApiError（status=null）
};
```

`reduce(state, action)` 处理以下几类 action：

- **服务端消息**：
  - `interviewer_sentence(transition=true)`：新建一条面试官消息（`done=false`），先放入过渡语。
  - `interviewer_delta`：追加到当前未完成的面试官消息；没有未完成的消息就新建一条。中文直接拼接，英文句子之间加一个空格。可以参考后端的 `separator_for`，写一个简单版本。
  - `interviewer_done`：用 `text` **整体替换**当前这条面试官消息，并标记 `done`；消息 id 用 `turn_id`。这样做可以去掉拼接带来的误差。
  - 非过渡语的 `interviewer_sentence`：忽略。它是给语音合成用的，内容和 delta 重复。
  - `state` → 更新 `activity`。
  - `stage` → 更新三个时间字段。
  - `error` → 设置 `error`。
  - `ended` → `connection = "ended"`。
  - `pong` → 不处理。
- **本地动作**：
  - `sent_text`：乐观地追加一条候选人消息；
  - `tick`：见下文“倒计时”；
  - `connection_changed`；
  - `clear_error`。
- **候选人消息 id**：用一个本地计数器生成；不要用 `Date.now()`，测试里不好控制。
- **解析服务端消息**：写成独立函数 `parseServerMessage(raw: string): TextServerMessage | null`。只检查 `type` 是否是已知值，以及必需字段的类型；不认识的消息返回 `null` 并忽略，不能让页面崩掉。

**倒计时**：`tick` action 每秒触发一次。只有同时满足 `connection === "open"`、`activity !== "paused"`、`phase` 不是 `closing` 或 `ended` 时，两个剩余时间才各减 1，最小为 0。收到 `stage` 事件就用服务端的值覆盖本地值。

**界面能做什么**：用纯函数 `controls(state)` 返回各按钮和输入框是否可用。规则如下，与后端的空操作规则保持一致：

| 控件 | 什么时候可用 |
| --- | --- |
| 输入框和发送 | 连接打开，且 `activity === "listening"` |
| 再说一遍题目 | 连接打开，`activity === "listening"`，且不在收尾阶段 |
| 跳过本题 | 同上 |
| 暂停 | 连接打开，`activity === "listening"`，且不在收尾阶段 |
| 继续 | 连接打开，且 `activity === "paused"` |
| 结束面试 | 连接打开，`activity` 为 `listening` 或 `paused`，且 `phase` 不是 `ended` |

面试官说话或思考时，所有按钮都禁用。这样可以避免命令排队，也避免候选人以为点了没反应。

## 2. 连接管理：`web/src/lib/interview/connection.ts`

- `createConnection({ url, onMessage, onStatus, WebSocketImpl = WebSocket, timers })` 返回 `{ send, close }`。WebSocket 实现和定时器都通过参数注入，测试时可以换成假的。
- **心跳**：连上后每 20 秒发一次 `ping`，断开后停止。
- **重连退避**：间隔为 1、2、4、8 秒，之后每次 10 秒；最多 10 次，用完后 `connection = "failed"`，界面显示“重新连接”按钮。连上后重试计数清零。
- **关闭码**：处理方式按上面“协议要点”里的表格；首次连接和重连收到 4409 的区别，按“必须处理的坑”第 3 条。决策写成纯函数 `closeDecision(code, { everConnected, ended }) → "report" | "fail" | "retry"`，并测试。
- **主动离开**：组件卸载时调用 `close()`。这次关闭要标记为主动关闭，不触发重连。
- **断线时的发送**：`send` 在连接未打开时返回 `false`，不缓存消息。界面在断线时本来就禁用了输入。
- 不要在控制台打印候选人回答或面试官文本。

## 3. 页面：`/interviews/[id]/text`

用一个客户端组件，里面的逻辑放进 hook `useInterviewSession(id)`，由它把 reducer 和 connection 连起来。

- **顶部栏**（固定在页面顶部）：
  - 当前阶段的中文名：`labels.ts` 增加 `phaseLabels satisfies Record<Phase, string>`，`ended` 显示为“已结束”；
  - 本阶段剩余和总剩余，格式 `mm:ss`；
  - 状态标签：聆听中 / 面试官思考中 / 面试官说话中 / 已暂停；
  - 连接状态：连接中、重连中（第 n 次）、已断开。
- **对话区**：
  - 面试官消息靠左，候选人消息靠右；
  - 面试官正在说话时，在未完成的消息末尾显示闪烁光标；
  - 有新消息时自动滚动到底部；但如果用户已经往上翻，就不要抢滚动位置（检查是否接近底部即可）；
  - 对话区设 `aria-live="polite"`。
- **输入区**：
  - 多行文本框，显示 `n / 4000` 字数；
  - Enter 发送，Shift+Enter 换行，输入法组字时按 Enter 不发送；
  - 只有空白时不能发送；
  - 不可输入时，在框内显示原因，例如“面试官正在说话…”或“已暂停，点继续后作答”。
- **按钮**：再说一遍题目、跳过本题、暂停/继续（同一个按钮切换）、结束面试。
  - 结束面试要二次确认：用原生 `<dialog>`，或者在页面内弹出确认框，文案是“结束后将生成报告，不能再继续这场面试”。不要用 `window.confirm`。
- **错误**：`error` 事件交给 `ErrorNotice` 显示（包括 trace_id），几秒后不要自动消失，由用户关闭。`invalid_message` 和 `too_many_messages` 正常情况下不会出现，出现就照常显示。
- **结束**：收到 `ended`，或关闭码为 1000 或 4410 时：
  - 在页面上显示“面试结束，正在前往报告…”；
  - 用 `router.replace` 跳转到 `/interviews/{id}/report`，不要用 `push`，这样点后退不会回到面试页。
- **离开提醒**：面试进行中（连接打开或重连中，且未结束）离开页面时，用 `beforeunload` 提醒；文案说明面试会保留，回来可以继续。
- **页面标题**：这一页导出 `metadata` 或在 layout 里设 `title`，内容为“文字面试 · 面试练习室”。如果页面是客户端组件，就在同目录加一个服务端组件 `layout.tsx` 来设置标题。
- **样式**：沿用 `lib/styles.ts` 和 Tailwind 工具类，不新增全局 CSS。

## 4. 测试（Vitest，node 环境）

至少覆盖：

- `reduce`：
  - 一次完整发言：`state=speaking` → 过渡语 → 两个 delta → `done` 整体替换，消息 id 是 `turn_id`；
  - 没有过渡语的发言（开场白）；
  - 非过渡语的 `sentence` 被忽略；
  - `stage` 覆盖本地倒计时；
  - `tick` 在暂停、断线、收尾时不减；剩余时间不会小于 0；
  - `ended` 把连接状态设为 ended；
  - `error` 转成 `ApiError`，并保留 trace_id。
- `controls`：上面表格里的每一行。
- `parseServerMessage`：合法消息、未知 `type`、缺字段、不是 JSON。
- `closeDecision`：每个关闭码，以及首次连接和重连两种情况下的 4409。
- `createConnection`：用假的 WebSocket 和假的定时器验证以下几点：
  - 心跳间隔是 20 秒；
  - 退避间隔依次为 1、2、4、8、10 秒，最多重试 10 次；
  - 主动 `close()` 后不重连；
  - 连上后重试计数清零。
- WebSocket 地址的转换：`http` 转 `ws`，`https` 转 `wss`。

## 5. 文档

- README 的“前端准备流程”一节补一句：生成计划后点“开始面试”进入文字面试，结束后跳到报告页（目前是占位页）。
- 有不明显的坑就记进 `docs/challenges.md` 和 `challenges-plain.md`，编号接在 C26 之后。StrictMode 双连接和 4409 的处理很可能值得记，没有就不写。

## 手动验证步骤（写进汇报，我会照着走一遍）

1. `make tunnel`、`make api`、`make web-dev`。用 `/new` 生成一份计划，点“开始面试”。
2. 面试官开场白逐句出现，状态从“说话中”变为“聆听中”；顶部阶段和倒计时在走。
3. 回答几轮，确认能看到过渡语、追问和换题。用输入法打中文时，组字过程中按 Enter 不会发送。
4. 点“暂停”，倒计时停止，输入框禁用；点“继续”，面试官重新提出当前问题。
5. 点“再说一遍题目”和“跳过本题”，确认面试官的行为符合预期。
6. 在面试中停掉 `make api` 再启动，页面应先显示“重连中”，然后自动恢复，并且保留之前的对话。
7. 刷新页面，看到续面提示，阶段和倒计时恢复。
8. 在另一个标签页打开同一场面试，那个标签页应显示“已在其他窗口打开”。
9. 点“结束面试”并确认，面试官说完收尾语后跳转到报告占位页；按浏览器后退不会回到面试页。再打开该面试的文字面试页，应直接跳到报告页（4410）。

## 交付时汇报

- 新增和修改的文件列表；手写代码行数，口径同 PR-23；超过 400 行时给出拆成 commit 的方案。
- `make check` 和 `make web-build` 的结果。
- 浏览器验证做到了哪些步骤，用的是假模型还是真实模型。
- 与本计划不一致的地方及理由；没做的事；风险与待确认事项，至少包括：后台标签页节流导致 4408；刷新后历史对话丢失。
