import { expect, it } from "vitest";
import {
  formatMinutes,
  groupQuestions,
  toPlanUpdate,
  validateQuestions,
} from "./plan";
import type { components } from "./api/schema";
type Question = components["schemas"]["Question"];
const question = (
  id: string,
  stage: Question["stage"] = "technical",
): Question => ({
  id,
  stage,
  text: "  为什么？  ",
  competency: "设计",
  project: "项目",
  key_points: [" 要点 ", "  "],
  follow_ups: [" 追问1 ", "", " 追问2 "],
});
it("只保留剩余题目和可编辑字段，去空白、空行且保持顺序", () => {
  expect(toPlanUpdate([question("q3"), question("q1")])).toEqual({
    questions: ["q3", "q1"].map((id) => ({
      id,
      text: "为什么？",
      key_points: ["要点"],
      follow_ups: ["追问1", "追问2"],
    })),
  });
});
it("分组按阶段顺序，同阶段保留题目原序", () => {
  expect(
    groupQuestions(
      [question("q3"), question("q2", "resume_deep_dive"), question("q1")],
      [
        { stage: "resume_deep_dive", seconds: 60, capacity: 1 },
        { stage: "technical", seconds: 120, capacity: 1 },
      ],
    ).map((group) => [group.stage, group.questions.map((q) => q.id)]),
  ).toEqual([
    ["resume_deep_dive", ["q2"]],
    ["technical", ["q3", "q1"]],
  ]);
});
it.each([
  [60, "1"],
  [90, "1.5"],
  [45, "1"],
  [0, "0"],
  [135, "2.5"],
])("%i 秒按半分钟格式化", (seconds, result) =>
  expect(formatMinutes(seconds)).toBe(result),
);
it("保存前检查正文、至少一个要点与 2–3 条追问", () => {
  expect(validateQuestions([question("q1")])).toEqual([]);
  expect(
    validateQuestions([
      { ...question("q1"), text: " ", key_points: [" "], follow_ups: ["a"] },
    ]),
  ).toHaveLength(3);
});
