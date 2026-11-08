import { describe, expect, it } from "vitest";
import { ApiError } from "../api/errors";
import type { components } from "../api/schema";
import { controls, initialSessionState, parseServerMessage, reduce, type SessionState } from "./session";
type ServerMessage = components["schemas"]["TextServerMessage"];
const receive = (state: SessionState, message: ServerMessage) =>
  reduce(state, { type: "server_message", message });
const stage: ServerMessage = { type: "stage", phase: "technical", remaining_s: 30, total_remaining_s: 90 };
const open = (): SessionState => reduce(initialSessionState, { type: "connection_changed", connection: "open" });

describe("session reducer", () => {
  it("combines transition and sentence deltas, then replaces text and id with done", () => {
    let state = receive(open(), { type: "state", value: "thinking" });
    state = receive(state, { type: "interviewer_sentence", text: "好的。", transition: true });
    state = receive(state, { type: "state", value: "speaking" });
    state = receive(state, { type: "interviewer_delta", text: "请解释。" });
    state = receive(state, { type: "interviewer_delta", text: "为什么？" });
    expect(state.messages).toEqual([{ id: "local-0", role: "interviewer", text: "好的。请解释。为什么？", done: false }]);
    state = receive(state, { type: "interviewer_done", turn_id: "turn-1", text: "服务端完整文本" });
    expect(state.messages).toEqual([{ id: "turn-1", role: "interviewer", text: "服务端完整文本", done: true }]);
    expect(state.activeInterviewerId).toBeNull();
  });
  it("creates an opening without transition and adds spaces between English sentences", () => {
    let state = receive(open(), { type: "state", value: "speaking" });
    state = receive(state, { type: "interviewer_delta", text: "Hello." });
    state = receive(state, { type: "interviewer_delta", text: "Tell me more." });
    expect(state.messages[0]?.text).toBe("Hello. Tell me more.");
    const ignored = receive(state, { type: "interviewer_sentence", text: "Tell me more.", transition: false });
    expect(ignored.messages).toBe(state.messages);
  });
  it("records a done-only utterance and assigns deterministic local candidate ids", () => {
    let state = receive(open(), { type: "interviewer_done", turn_id: "t", text: "完成" });
    state = reduce(state, { type: "sent_text", text: "回答" });
    state = reduce(state, { type: "sent_text", text: "补充" });
    expect(state.messages).toEqual([
      { id: "t", role: "interviewer", text: "完成", done: true },
      { id: "local-0", role: "candidate", text: "回答" },
      { id: "local-1", role: "candidate", text: "补充" },
    ]);
  });
  it("uses stage values to overwrite local countdown, which never goes below zero", () => {
    let state = receive(receive(open(), { type: "state", value: "listening" }), stage);
    state = reduce(state, { type: "tick" });
    expect([state.stageRemainingS, state.totalRemainingS]).toEqual([29, 89]);
    state = receive(state, { ...stage, remaining_s: 1, total_remaining_s: 0 });
    state = reduce(reduce(state, { type: "tick" }), { type: "tick" });
    expect([state.stageRemainingS, state.totalRemainingS]).toEqual([0, 0]);
    expect(reduce(open(), { type: "tick" }).stageRemainingS).toBeNull();
  });
  it.each([
    { activity: "paused" as const },
    { connection: "reconnecting" as const },
    { connection: "failed" as const },
    { phase: "closing" as const },
    { phase: "ended" as const },
  ])("does not count down while %j", (override) => {
    const state = { ...receive(open(), stage), ...override };
    const ticked = reduce(state, { type: "tick" });
    expect([ticked.stageRemainingS, ticked.totalRemainingS]).toEqual([30, 90]);
  });
  it("ends and preserves error message/code/trace id until dismissed", () => {
    const state = receive(open(), { type: "error", code: "too_many_messages", message: "请等一等", trace_id: "trace-1" });
    expect(state.error).toBeInstanceOf(ApiError);
    expect(state.error).toMatchObject({ status: null, code: "too_many_messages", message: "请等一等", traceId: "trace-1" });
    expect(receive(state, { type: "pong" }).error).toBe(state.error);
    expect(reduce(state, { type: "clear_error" }).error).toBeNull();
    expect(receive(state, { type: "ended" }).connection).toBe("ended");
  });
  it("distinguishes fresh stage -> speaking from snapshot stage -> listening or paused", () => {
    for (const value of ["listening", "paused"] as const) {
      let state = receive(open(), { type: "pong" });
      state = receive(state, stage);
      expect(state.resumed).toBe(false);
      expect(receive(state, { type: "state", value }).resumed).toBe(true);
    }
    let fresh = receive(open(), { ...stage, phase: "opening" });
    fresh = receive(fresh, { type: "state", value: "speaking" });
    fresh = receive(fresh, { type: "state", value: "listening" });
    expect(fresh.resumed).toBe(false);
  });
  it("preserves interrupted text but does not merge a new turn after reconnecting", () => {
    let state = receive(open(), { type: "interviewer_delta", text: "中断的句子" });
    state = reduce(state, { type: "connection_changed", connection: "reconnecting" });
    expect(state.activeInterviewerId).toBeNull();
    state = reduce(state, { type: "connection_changed", connection: "open" });
    expect(state.activity).toBeNull();
    state = receive(receive(state, stage), { type: "state", value: "listening" });
    expect(state.resumed).toBe(false);
    state = receive(state, { type: "interviewer_delta", text: "新问题" });
    state = receive(state, { type: "interviewer_done", turn_id: "new-turn", text: "新问题" });
    expect(state.messages.map(({ text }) => text)).toEqual(["中断的句子", "新问题"]);
    expect(state.messages[1]?.id).toBe("new-turn");
  });
});

describe("controls", () => {
  const allOff = { input: false, send: false, repeat: false, skip: false, pause: false, resume: false, end: false };
  it("enables answering and commands while listening", () => {
    expect(controls({ ...open(), activity: "listening", phase: "technical" })).toEqual({ ...allOff, input: true, send: true, repeat: true, skip: true, pause: true, end: true });
  });
  it("only enables resume and end while paused", () => {
    expect(controls({ ...open(), activity: "paused", phase: "technical" })).toEqual({ ...allOff, resume: true, end: true });
  });
  it("disables repeat, skip and pause during closing", () => {
    expect(controls({ ...open(), activity: "listening", phase: "closing" })).toEqual({ ...allOff, input: true, send: true, end: true });
  });
  it.each(["thinking", "speaking", null] as const)("disables everything for activity %s", (activity) => {
    expect(controls({ ...open(), activity })).toEqual(allOff);
  });
  it.each(["connecting", "reconnecting", "failed", "ended"] as const)("disables everything when connection is %s", (connection) => {
    expect(controls({ ...open(), connection, activity: "listening" })).toEqual(allOff);
  });
  it("never enables end after the ended stage", () => {
    expect(controls({ ...open(), activity: "listening", phase: "ended" }).end).toBe(false);
  });
});

describe("parseServerMessage", () => {
  it.each<ServerMessage>([
    { type: "interviewer_delta", text: "内容" },
    { type: "interviewer_sentence", text: "内容", transition: true },
    { type: "interviewer_done", text: "内容", turn_id: "t" },
    { type: "state", value: "speaking" }, stage,
    { type: "error", code: "bad", message: "错误", trace_id: "trace" },
    { type: "ended" }, { type: "pong" },
  ])("accepts valid $type", (message) => {
    expect(parseServerMessage(JSON.stringify(message))).toEqual(message);
  });
  it.each([
    "not json", "null", "[]", '"stage"', "1", '{}',
    '{"type":"unknown"}', '{"type":"interviewer_delta","text":1}',
    '{"type":"interviewer_sentence","text":"x"}',
    '{"type":"interviewer_sentence","text":"x","transition":"true"}',
    '{"type":"interviewer_done","text":"x"}',
    '{"type":"stage","phase":"unknown","remaining_s":1,"total_remaining_s":2}',
    '{"type":"stage","phase":"technical","remaining_s":"1","total_remaining_s":2}',
    '{"type":"state","value":"unknown"}',
    '{"type":"error","code":"x","message":"bad"}',
  ])("ignores malformed message %s", (raw) => expect(parseServerMessage(raw)).toBeNull());
});

it("ignores a repeated open status without discarding activity or active speech", () => {
  let state = receive(open(), { type: "state", value: "speaking" });
  state = receive(state, { type: "interviewer_delta", text: "问题" });
  expect(reduce(state, { type: "connection_changed", connection: "open" })).toBe(state);
});
