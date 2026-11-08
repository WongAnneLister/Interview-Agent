# PR-25 前端基础报告页 — 执行计划

> 给执行代码的 agent：照这份计划实现，**不要 commit**。完成后跑 `make check`（全部通过）。
> 结尾按“交付时汇报”一节列出与计划不一致的地方。

## 先读

- `CLAUDE.md`（全部约定都适用；前端同样要求类型安全、无 `any`、所有请求有超时）
- `docs/Claude-Code开工执行文档.md` 的 PR-25 段落
- 后端：
  - `backend/src/interview/api/routes/interviews.py`：`ReportOut`、`_report_view`、`GET/POST /{id}/report`、`_owned_interview`
  - `backend/src/interview/domain/report_models.py`：`ReportBody`、`CompetencyReview`、`QuestionReview`、`REPORT_DISCLAIMER`
  - `backend/src/interview/domain/evaluator.py`：`EvidenceQuote`（turn_id / quote / start_ms / end_ms）、`normalize_quote`
  - `backend/src/interview/domain/report_store.py` 的 `load()`：按 user_id 读 turns 的写法
  - `backend/src/interview/db/models.py`：`Turn`
  - `backend/tests/unit/test_report_api.py`：SQLite + 假厂商的接口测试写法
- 前端：
  - `web/src/lib/api/client.ts`（`request()` + 超时）、`errors.ts`
  - `web/src/lib/request.ts`、`useRequest.ts`、`styles.ts`、`labels.ts`
  - `web/src/app/interviews/[id]/plan/page.tsx`（客户端页面写法）、`text/layout.tsx`（客户端页面的 metadata 放 layout）
  - `web/src/components/ErrorNotice.tsx`、`LoadingNotice.tsx`
  - `web/src/lib/interview/connection.ts`：面试结束后跳到 `/interviews/[id]/report`

## 范围

要做：

1. 页面 `/interviews/[id]/report`：轮询报告状态；pending 显示生成中，failed 提供“重新生成”。
2. 就绪后显示：总体总结与 3 条改进建议；各能力项得分（1–5，“未考察到”单独显示）及引用原话；逐题的摘要、优点、缺失要点、参考说法；完整转写（可折叠，默认折叠）。
3. 点击引用：展开转写、滚动到对应发言、高亮引用的那段话。
4. 页面底部显示后端返回的 `disclaimer`（内容即“AI 生成，仅供练习参考……”，不要在前端另写一份文案）。

不做：录音回放与时间点跳转（PR-35）、表达反馈 R4、报告导出/分享、生成进度百分比（后端没有暴露进度，只显示已等待秒数）、报告页的面试设置信息。

### 与开工文档不一致处（需要在汇报里写明）

报告接口 `ReportOut` 里**没有转写**，后端也没有任何返回 turns 的接口，而第 2、3 条都需要转写。
因此本 PR 增加一个只读后端接口 `GET /api/interviews/{id}/transcript`（见下文 A 部分）。不要把转写塞进 `ReportBody`（那是持久化的 summary_json 结构，改它会影响 PR-21 的存储和校验）。

## 拆成两个 commit（各自 ≤ 400 行，不含 `openapi.json`、`schema.d.ts`）

- **part 1**：A（后端转写接口 + 测试 + `make gen-types`）和 B（前端纯函数 + Vitest + API 客户端函数）
- **part 2**：C（hook、组件、页面）

执行 agent 只写代码不提交，但请按这个边界组织改动，并在汇报里给出每部分的行数（`git diff --stat`）。

---

## A. 后端：转写接口

文件：`backend/src/interview/api/routes/interviews.py`（如果文件因此超过约 400 行，就新建 `routes/transcript.py` 并在 `api/app.py` 里 `include_router`，前缀和 tag 与 interviews 一致）。

```python
class TranscriptTurnOut(BaseModel):
    id: uuid.UUID
    seq: int
    role: TurnRole            # interviewer / candidate
    text: str
    start_ms: int
    end_ms: int
    question_id: str | None

class TranscriptOut(BaseModel):
    interview_id: uuid.UUID
    turns: list[TranscriptTurnOut]

@router.get("/{interview_id}/transcript")
async def get_transcript(interview_id, user_id: CurrentUserId, session: DbSession) -> TranscriptOut
```

规则：

- 先 `_owned_interview(session, user_id, interview_id)`，不属于本用户或不存在 → 404（与报告接口同一种错误）。
- 用 `TurnRepository(session, user_id).select().where(Turn.interview_id == interview_id).order_by(Turn.seq)` 读取，必须经过 user_id 过滤的仓储，不要裸 `select(Turn)`。
- 面试未结束（不是 completed / aborted）→ 409 `ConflictError("面试结束后才可查看转写")`。报告页只在结束后使用；进行中的转写不对外暴露，避免和 WebSocket 状态不一致。
- 没有 turns 时返回空列表，不报错。
- 不在日志里输出 text。
- 测试（加到 `tests/unit/test_report_api.py` 或新建 `test_transcript_api.py`，沿用现有 SQLite fixture）：
  1. 已结束面试按 seq 升序返回全部字段；
  2. 其他用户的面试 → 404；
  3. 进行中的面试 → 409；
  4. 没有发言 → `turns == []`。
- 跑 `make gen-types` 更新 `web/openapi.json` 和 `web/src/lib/api/schema.d.ts`（`test_openapi_snapshot` 会检查）。

## B. 前端：API 函数与纯逻辑

### B1. `web/src/lib/api/client.ts` 新增

```ts
export function getReport(id: string, signal?: AbortSignal)        // GET  /api/interviews/{interview_id}/report
export function regenerateReport(id: string, signal?: AbortSignal) // POST /api/interviews/{interview_id}/report
export function getTranscript(id: string, signal?: AbortSignal)    // GET  /api/interviews/{interview_id}/transcript
```

都用 `TIMEOUT_MS.standard`，写法照搬 `getPlan`。

### B2. `web/src/lib/report.ts`（与 React 无关，全部有单元测试 `report.test.ts`）

类型全部从 `components["schemas"]` 取，不要手写重复的接口类型。

1. **轮询决策** `nextPoll(state, outcome)`：输入当前尝试次数和这次结果，输出 `{ kind: "wait", delayMs } | { kind: "done" } | { kind: "failed" } | { kind: "error", error } | { kind: "not_ended" }`。
   - `pending` → 等待后再查；间隔从 1500ms 起每次 ×1.5，上限 5000ms。
   - `ready` → done；`failed` → failed（后端已经把超时的 pending 折算成 failed，前端不另设总时限）。
   - 404 且 code 为 `report_not_found` → `not_ended`（面试还没结束或报告从未登记）。
   - 网络错误 / 5xx：连续 3 次以内继续按退避等待，第 4 次给出 error；一次成功就清零。
   - 其他 4xx（含越权的 404）→ 直接 error。
2. **能力项得分展示** `formatScore(score: number | null)`：`null` → `"未考察到"`；否则保留一位小数，如 `"3.5 / 5"`。`overall_score` 同理（null 显示“未考察到”）。
3. **逐题分组**：`asked === false` 的题目单独归为“本场未问到”，放在已问题目之后，只显示题目文字，不显示空的反馈块。
4. **引用定位** `locateQuote(turnText: string, quote: string): { start: number; end: number } | null`：
   - 先在原文里精确查找；
   - 找不到时，按与后端 `normalize_quote` 一致的规则（NFKC、小写、去掉空白和标点）建立“归一化下标 → 原文下标”的映射再查找，返回原文里的区间；
   - 仍找不到返回 `null`（调用方退化为高亮整条发言）。
   - 后端规则（`evaluator.py:191`）：`NFKC` → `casefold()` → 去掉空白和 Unicode 类别以 `P` 开头的字符，符号（如 C++ 的 `+`）保留。前端对应写法：逐字符 `normalize("NFKC")` 后 `toLowerCase()`，用 `/\s/u` 和 `/\p{P}/u` 过滤。`toLowerCase` 与 `casefold` 在 ß 等少数字符上不同，在注释里注明；因为找不到时有整条高亮兜底，不必追求完全一致。
   - 逐字符做 NFKC 可能一变多（如 `㎏` → `kg`），映射时要让每个产出字符都指回同一个原文下标。
5. **时间格式** `formatOffset(ms)`：`mm:ss`，超过一小时 `h:mm:ss`。
6. **转写里找发言** `turnIndex(turns)`：`Map<turn_id, turn>`；引用的 turn_id 不在转写里时，引用照常显示但不可点击（按钮 `disabled`，title 说明“转写中找不到这句话”）。

测试至少覆盖：轮询的每个分支（含退避上限、错误计数清零）、`formatScore` 的 null / 整数 / 小数、未问到题目的分组与顺序、`locateQuote` 精确命中 / 只差全角标点或大小写也能命中 / 找不到、`formatOffset` 边界（0、59_999、3_600_000）。

## C. 前端：hook、组件、页面

### C1. `web/src/lib/report/useReport.ts`（或 `web/src/lib/useReport.ts`，与现有目录风格一致即可）

- 挂载时 `getReport`，按 `nextPoll` 用 `setTimeout` 轮询；卸载或 id 变化时取消计时器并 abort 正在进行的请求（用 `AbortController`）。
- 状态 `ready` 后再请求一次 `getTranscript`（只在 ready 时取，失败单独显示错误和重试按钮，不影响报告主体显示）。
- `regenerate()`：调 `regenerateReport`；202 → 回到轮询；409（已经在生成）→ 也回到轮询；其他错误显示。
- 页面不可见时不需要特殊处理（v0 本机使用），不要引入额外依赖。

### C2. 组件（`web/src/components/report/`）

- `ReportSummary`：总分、`overall`、`top_improvements`（有序列表）；`degraded` 非空时显示一条提示“部分题目评分失败，以下结果不完整”，并列出条目。
- `CompetencyList`：每项名称、权重、得分（`formatScore`）、结论、引用列表；“未考察到”的能力项单独放在一组（标题“本场未考察到”），不显示空分数条。
- `QuestionFeedback`：每题题目、所属能力项、得分、摘要、优点、缺失要点、参考说法、引用列表；未问到的题目按 B2-3 处理。
- `QuoteButton`：显示“原话 + 时间点”，是 `<button type="button">`，点击调用 `onSelect(turnId, quote)`。
- `TranscriptPanel`：受控的折叠面板（不要用 `<details>`，因为点击引用时需要程序化展开）；标题按钮带 `aria-expanded` 和 `aria-controls`。每条发言显示角色（面试官 / 候选人，加到 `labels.ts`，用 `satisfies Record<TurnRole, string>`）、时间点和原文。被选中的发言：
  - 加明显的背景色和左边框；
  - 用 `<mark>` 包住 `locateQuote` 找到的区间，找不到就整条高亮；
  - 元素 `tabIndex={-1}`，展开后 `scrollIntoView({ block: "center" })` 并 `focus({ preventScroll: true })`，方便键盘用户；
  - 再点同一条引用要能重新滚动过去；点另一条引用切换高亮。
- 颜色、按钮、面板全部用 `lib/styles.ts` 现有的类和颜色，不新增设计体系。

### C3. 页面

- `web/src/app/interviews/[id]/report/page.tsx` 改成 `"use client"` 页面；`metadata` 挪到新建的 `report/layout.tsx`（照 `text/layout.tsx`）。
- 各状态：
  - 首次加载：`LoadingNotice message="正在读取报告…"`
  - pending：`LoadingNotice message="报告生成中，通常一分钟内完成"`（它自带已等待秒数和 `role="status"`）
  - failed：说明“报告生成失败”，有 `error` 时显示原因码，按钮“重新生成”（`buttonPrimary`，请求中禁用）
  - not_ended：“面试还没有结束，结束后才会生成报告”，链接到 `/interviews/[id]/text`
  - error：`ErrorNotice`（带 trace_id）+ 重试
  - ready：Summary → 能力项 → 逐题反馈 → 转写面板
- 页面底部始终显示 `disclaimer`（ready 时用接口返回值；其他状态不显示报告内容，可以不显示声明）。
- 保留现有“返回，准备新的面试”链接。

## 验证

1. `make check` 全部通过（包括 `test_openapi_snapshot`、前端 lint / tsc / vitest）。
2. 写出手动验证步骤（我来跑）：后端 + 前端启动，用文字面试做完一场 → 自动跳到报告页 → 看到生成中和等待秒数 → 就绪后各块内容正确 → 点引用后转写展开、滚动、高亮 → 刷新页面直接显示就绪报告 → 打开一个进行中面试的报告页显示 not_ended。
3. 失败分支的手动验证：临时把百炼 key 改错（或断网）让报告失败，看到“重新生成”，恢复后点击能重新进入生成中。

## 交付时汇报

- 改动文件列表，part 1 / part 2 各自的行数（不含 `openapi.json`、`schema.d.ts`）。
- 与本计划不一致的地方及原因（特别是：后端接口放在哪个文件、`locateQuote` 的归一化规则与后端是否完全一致）。
- `make check` 的结果。
- 没做的东西和发现的问题（例如后端字段不够用的地方）。
