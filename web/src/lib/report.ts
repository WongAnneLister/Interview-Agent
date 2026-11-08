import { ApiError } from "./api/errors";
import type { components } from "./api/schema";

type S = components["schemas"];
export type PollState = { attempt: number; errors: number };
export type PollDecision =
  | { kind: "wait"; delayMs: number; state: PollState }
  | { kind: "done" | "failed" | "not_ended" }
  | { kind: "error"; error: ApiError };

export function nextPoll(state: PollState, outcome: S["ReportOut"] | ApiError): PollDecision {
  let errors = 0;
  if (outcome instanceof ApiError) {
    if (outcome.status === 404 && outcome.code === "report_not_found") {
      return { kind: "not_ended" };
    }
    errors = state.errors + 1;
    if ((outcome.status !== null && outcome.status < 500) || errors > 3) {
      return { kind: "error", error: outcome };
    }
  } else if (outcome.status !== "pending") {
    return { kind: outcome.status === "ready" ? "done" : "failed" };
  }
  return {
    kind: "wait",
    delayMs: Math.min(1500 * 1.5 ** state.attempt, 5000),
    state: { attempt: state.attempt + 1, errors },
  };
}

export function formatScore(score: number | null): string {
  return score === null ? "未考察到" : `${score.toFixed(1)} / 5`;
}

export function groupQuestions(questions: S["QuestionReview"][]) {
  return {
    asked: questions.filter(q => q.asked), unasked: questions.filter(q => !q.asked),
  };
}

export function formatOffset(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const seconds = String(total % 60).padStart(2, "0");
  const minutes = String(Math.floor(total / 60) % 60).padStart(2, "0");
  return total < 3600
    ? `${minutes}:${seconds}` : `${Math.floor(total / 3600)}:${minutes}:${seconds}`;
}

export function turnIndex(turns: S["TranscriptTurnOut"][]) {
  return new Map(turns.map(turn => [turn.id, turn]));
}

function normalized(text: string) {
  let value = "";
  let offset = 0;
  const ranges: { start: number; end: number }[] = [];
  for (const char of text) {
    // toLowerCase differs from backend casefold for e.g. ß;
    // caller highlights the whole turn on a miss.
    for (const output of char.normalize("NFKC").toLowerCase()) {
      if (/\s|\p{P}/u.test(output)) continue;
      value += output;
      for (let i = 0; i < output.length; i++) {
        ranges.push({ start: offset, end: offset + char.length });
      }
    }
    offset += char.length;
  }
  return { value, ranges };
}

export function locateQuote(
  turnText: string, quote: string,
): { start: number; end: number } | null {
  if (!quote) return null;
  const exact = turnText.indexOf(quote);
  if (exact >= 0) return { start: exact, end: exact + quote.length };
  const source = normalized(turnText);
  const target = normalized(quote).value;
  if (!target) return null;
  const index = source.value.indexOf(target);
  const first = source.ranges[index];
  const last = source.ranges[index + target.length - 1];
  return index >= 0 && first && last ? { start: first.start, end: last.end } : null;
}
