import type { components } from "./api/schema";
type Question = components["schemas"]["Question"];
type StageSlot = components["schemas"]["StageSlot"];
const cleanLines = (lines: string[]) =>
  lines.map((line) => line.trim()).filter(Boolean);

export function toPlanUpdate(
  questions: Question[],
): components["schemas"]["PlanUpdate"] {
  return {
    questions: questions.map(({ id, text, key_points, follow_ups }) => ({
      id,
      text: text.trim(),
      key_points: cleanLines(key_points),
      follow_ups: cleanLines(follow_ups),
    })),
  };
}

export function groupQuestions(questions: Question[], stages: StageSlot[]) {
  return stages.map(({ stage }) => ({
    stage,
    questions: questions.filter((question) => question.stage === stage),
  }));
}

export function formatMinutes(seconds: number): string {
  return String(Math.round(seconds / 30) / 2);
}

export function validateQuestions(questions: Question[]): string[] {
  return toPlanUpdate(questions).questions.flatMap((q, i) => [
    ...(!q.text ? [`第 ${i + 1} 题：题目正文不能为空`] : []),
    ...(q.key_points.length < 1
      ? [`第 ${i + 1} 题：至少保留一条考察要点`]
      : []),
    ...(q.follow_ups.length < 2 || q.follow_ups.length > 3
      ? [`第 ${i + 1} 题：追问需要 2–3 条`]
      : []),
  ]);
}
