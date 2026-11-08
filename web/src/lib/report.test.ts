import { expect, it } from "vitest";
import { ApiError } from "./api/errors";
import type { components } from "./api/schema";
import {
  nextPoll, formatScore, groupQuestions, locateQuote, formatOffset, turnIndex,
} from "./report";

type S = components["schemas"];
const response = (status: S["ReportStatus"]): S["ReportOut"] => ({
  status, interview_id: "id", generated_at: null, model: null,
  prompt_version: null, report: null, error: null, disclaimer: "声明",
});
const initial = { attempt: 0, errors: 0 };

it("pending 退避从1500到5000，ready/failed停止", () => {
  expect(nextPoll(initial, response("pending"))).toMatchObject({ kind: "wait", delayMs: 1500 });
  expect(nextPoll({ attempt: 1, errors: 0 }, response("pending"))).toMatchObject({ delayMs: 2250 });
  expect(nextPoll({ attempt: 20, errors: 0 }, response("pending"))).toMatchObject({
    delayMs: 5000,
  });
  expect(nextPoll(initial, response("ready"))).toEqual({ kind: "done" });
  expect(nextPoll(initial, response("failed"))).toEqual({ kind: "failed" });
});

it.each([null, 500, 503])("网络或%s错误第四次停止，成功pending清零", status => {
  const error = new ApiError("错误", status, "network_error", "trace");
  for (const errors of [0, 1, 2]) {
    expect(nextPoll({ attempt: errors, errors }, error)).toMatchObject({
      kind: "wait", state: { errors: errors + 1 },
    });
  }
  expect(nextPoll({ attempt: 3, errors: 3 }, error)).toEqual({ kind: "error", error });
  expect(nextPoll({ attempt: 3, errors: 3 }, response("pending"))).toMatchObject({
    state: { errors: 0 },
  });
});

it("仅report_not_found为未结束，其他4xx立即错误", () => {
  expect(nextPoll(initial, new ApiError("未登记", 404, "report_not_found"))).toEqual({
    kind: "not_ended",
  });
  for (const status of [400, 401, 403, 404, 409, 422]) {
    const error = new ApiError("拒绝", status, "not_found");
    expect(nextPoll(initial, error)).toEqual({ kind: "error", error });
  }
});

it.each([[null, "未考察到"], [3, "3.0 / 5"], [3.5, "3.5 / 5"]])("得分%s", (score, expected) => {
  expect(formatScore(score as number | null)).toBe(expected);
});

it.each([
  [0, "00:00"], [59999, "00:59"], [60000, "01:00"],
  [3600000, "1:00:00"], [3661000, "1:01:01"],
])("时间%s", (ms, expected) => {
  expect(formatOffset(ms as number)).toBe(expected);
});

it("未问题目后置且各组保持原顺序", () => {
  const question = (id: string, asked: boolean): S["QuestionReview"] => ({
    question_id: id, asked, question: id, competency: "能力", summary: "", improved_answer: "",
  });
  const questions = [
    question("u1", false), question("a1", true), question("u2", false), question("a2", true),
  ];
  expect(groupQuestions(questions)).toEqual({
    asked: [questions[1], questions[3]], unasked: [questions[0], questions[2]],
  });
});

it.each([
  ["一句原话。", "原话", { start: 2, end: 4 }],
  ["用了 Ｃ＋＋，并发。", "c++ 并发", { start: 3, end: 9 }],
  ["重量为㎏。", "KG", { start: 3, end: 4 }],
  ["😀ABC", "abc", { start: 2, end: 5 }],
  ["原文", "不存在", null],
  ["原文", "", null],
  ["Straße", "strasse", null],
])("引用定位 %s / %s", (text, quote, expected) => {
  expect(locateQuote(text, quote)).toEqual(expected);
});

it("转写索引按id查找，缺失返回undefined", () => {
  const turn: S["TranscriptTurnOut"] = {
    id: "t", seq: 1, role: "candidate", text: "回答",
    start_ms: 0, end_ms: 1000, question_id: null,
  };
  expect(turnIndex([turn]).get("t")).toBe(turn);
  expect(turnIndex([turn]).get("missing")).toBeUndefined();
});
