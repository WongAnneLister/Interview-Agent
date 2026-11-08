import { ApiError } from "../api/errors";
import type { components } from "../api/schema";
export type TextServerMessage = components["schemas"]["TextServerMessage"];
type Phase = components["schemas"]["Phase"];
export type Message =
  | { id: string; role: "interviewer"; text: string; done: boolean }
  | { id: string; role: "candidate"; text: string };
export type Connection =
  | "connecting"
  | "open"
  | "reconnecting"
  | "ended"
  | "failed";
export type SessionState = {
  connection: Connection;
  activity: components["schemas"]["StateEvent"]["value"] | null;
  phase: Phase | null;
  stageRemainingS: number | null;
  totalRemainingS: number | null;
  messages: Message[];
  resumed: boolean;
  error: ApiError | null;
  nextMessageId: number;
  activeInterviewerId: string | null;
  initialEvent: "waiting" | "stage" | "received";
};
export const initialSessionState: SessionState = {
  connection: "connecting",
  activity: null,
  phase: null,
  stageRemainingS: null,
  totalRemainingS: null,
  messages: [],
  resumed: false,
  error: null,
  nextMessageId: 0,
  activeInterviewerId: null,
  initialEvent: "waiting",
};
export type SessionAction =
  | { type: "server_message"; message: TextServerMessage }
  | { type: "sent_text"; text: string }
  | { type: "tick" }
  | { type: "connection_changed"; connection: Connection }
  | { type: "clear_error" };

function appendText(previous: string, text: string): string {
  // 协议里没有语言字段：含汉字时直接拼接，英文句子之间补一个空格。
  const needsSpace =
    previous !== "" &&
    text !== "" &&
    !/\s$/.test(previous) &&
    !/^\s/.test(text) &&
    !/\p{Script=Han}/u.test(previous + text);
  return previous + (needsSpace ? " " : "") + text;
}
function interviewerMessage(
  state: SessionState,
  text: string,
  turnId?: string,
): SessionState {
  const index = state.messages.findIndex(
    (message) => message.id === state.activeInterviewerId,
  );
  const previous = state.messages[index];
  const current = previous?.role === "interviewer" ? previous : null;
  const id = turnId ?? current?.id ?? `local-${state.nextMessageId}`;
  const message: Message = {
    id,
    role: "interviewer",
    // interviewer_done 的完整文本整体替换，消掉逐句拼接的误差
    text: turnId ? text : appendText(current?.text ?? "", text),
    done: turnId !== undefined,
  };
  const messages = [...state.messages];
  if (current) messages[index] = message;
  else messages.push(message);
  return {
    ...state,
    messages,
    activeInterviewerId: turnId ? null : id,
    nextMessageId: state.nextMessageId + (!current && !turnId ? 1 : 0),
  };
}

export function reduce(
  state: SessionState,
  action: SessionAction,
): SessionState {
  switch (action.type) {
    case "sent_text":
      return {
        ...state,
        nextMessageId: state.nextMessageId + 1,
        messages: [
          ...state.messages,
          {
            id: `local-${state.nextMessageId}`,
            role: "candidate",
            text: action.text,
          },
        ],
      };
    case "clear_error":
      return { ...state, error: null };
    case "connection_changed":
      if (action.connection === state.connection) return state;
      return {
        ...state,
        connection: action.connection,
        activeInterviewerId: null,
        // 新连接上要等服务端重新告知状态，期间按钮全部禁用
        ...(action.connection === "open"
          ? { activity: null, initialEvent: "waiting" }
          : {}),
      };
    case "tick": {
      const frozen =
        state.connection !== "open" ||
        state.activity === "paused" ||
        state.phase === "closing" ||
        state.phase === "ended";
      if (frozen) return state;
      const decrement = (value: number | null) =>
        value === null ? null : Math.max(0, value - 1);
      return {
        ...state,
        stageRemainingS: decrement(state.stageRemainingS),
        totalRemainingS: decrement(state.totalRemainingS),
      };
    }
    case "server_message": {
      const message = action.message;
      if (message.type === "pong") return state;
      if (message.type === "error") {
        return {
          ...state,
          error: new ApiError(
            message.message,
            null,
            message.code,
            message.trace_id,
          ),
        };
      }
      let next = state;
      if (state.initialEvent === "waiting") {
        next = {
          ...state,
          initialEvent: message.type === "stage" ? "stage" : "received",
        };
      }
      // start() 和 snapshot() 都先发 stage：之后是 speaking 为新开场，
      // listening / paused 且没有对话记录为续面。
      if (message.type === "state" && state.initialEvent === "stage") {
        next = {
          ...next,
          initialEvent: "received",
          resumed:
            state.messages.length === 0 &&
            (message.value === "listening" || message.value === "paused"),
        };
      }
      switch (message.type) {
        case "interviewer_sentence":
          // 过渡语没有对应的 delta，单独开一条；其余 sentence 与 delta 重复（给语音合成用）
          return message.transition
            ? interviewerMessage(
                { ...next, activeInterviewerId: null },
                message.text,
              )
            : next;
        case "interviewer_delta":
          return interviewerMessage(next, message.text);
        case "interviewer_done":
          return interviewerMessage(next, message.text, message.turn_id);
        case "state":
          return { ...next, activity: message.value };
        case "stage":
          return {
            ...next,
            phase: message.phase,
            stageRemainingS: message.remaining_s,
            totalRemainingS: message.total_remaining_s,
          };
        case "ended":
          return { ...next, connection: "ended", activeInterviewerId: null };
      }
    }
  }
}

export function controls(state: SessionState) {
  const open = state.connection === "open";
  const listening = open && state.activity === "listening";
  const paused = open && state.activity === "paused";
  const question =
    listening && state.phase !== "closing" && state.phase !== "ended";
  return {
    input: listening,
    send: listening,
    repeat: question,
    skip: question,
    pause: question,
    resume: paused,
    end: (listening || paused) && state.phase !== "ended",
  };
}

const phases: string[] = [
  "opening",
  "self_intro",
  "resume_deep_dive",
  "technical",
  "behavioral",
  "candidate_questions",
  "closing",
  "ended",
];
const activities: string[] = ["listening", "thinking", "speaking", "paused"];
export function parseServerMessage(raw: string): TextServerMessage | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const message = value as Record<string, unknown>;
  let valid = false;
  switch (message.type) {
    case "interviewer_delta":
      valid = typeof message.text === "string";
      break;
    case "interviewer_sentence":
      valid =
        typeof message.text === "string" &&
        typeof message.transition === "boolean";
      break;
    case "interviewer_done":
      valid =
        typeof message.text === "string" &&
        typeof message.turn_id === "string";
      break;
    case "state":
      valid = typeof message.value === "string" && activities.includes(message.value);
      break;
    case "stage":
      valid =
        typeof message.phase === "string" &&
        phases.includes(message.phase) &&
        typeof message.remaining_s === "number" &&
        Number.isFinite(message.remaining_s) &&
        typeof message.total_remaining_s === "number" &&
        Number.isFinite(message.total_remaining_s);
      break;
    case "error":
      valid = [message.code, message.message, message.trace_id].every(
        (field) => typeof field === "string",
      );
      break;
    case "ended":
    case "pong":
      valid = true;
  }
  return valid ? value as TextServerMessage : null;
}
