import { describe, expect, it } from "vitest";
import { groupHistory, type HistoryQuestion } from "./history";

function item(
  overrides: Partial<HistoryQuestion>,
  question: Partial<HistoryQuestion["question"]> = {},
): HistoryQuestion {
  return {
    interview_id: "i1",
    job_title: "后端工程师",
    interview_type: "technical",
    language: "zh",
    status: "completed",
    created_at: "2026-11-10T08:00:00Z",
    ...overrides,
    question: {
      id: "q1",
      stage: "technical",
      competency: "系统设计",
      project: "",
      text: "怎么设计一个限流器？",
      key_points: ["令牌桶"],
      follow_ups: ["分布式下怎么做", "突发流量"],
      ...question,
    },
  };
}

const items = [
  item({ created_at: "2026-11-01T00:00:00Z" }, { id: "q1" }),
  item(
    { interview_id: "i2", job_title: "AI 工程师", created_at: "2026-11-14T00:00:00Z" },
    { id: "q2", stage: "resume_deep_dive", competency: "LLM 应用", text: "讲讲你的 RAG 项目" },
  ),
  item(
    { interview_id: "i3", created_at: "2026-11-05T00:00:00Z" },
    { id: "q3", stage: "behavioral", competency: "系统设计", text: "说一次线上事故" },
  ),
];

describe("groupHistory", () => {
  it("按岗位分组，最近练过的岗位在前，组内新题在前", () => {
    const groups = groupHistory(items, "job");
    expect(groups.map((g) => g.title)).toEqual(["AI 工程师", "后端工程师"]);
    expect(groups[1]?.items.map((i) => i.question.id)).toEqual(["q3", "q1"]);
  });

  it("按题型分组时按面试阶段的顺序排", () => {
    const groups = groupHistory(items, "stage");
    expect(groups.map((g) => g.title)).toEqual(["简历深挖", "技术问题", "行为问题"]);
  });

  it("按能力分组时题多的在前", () => {
    const groups = groupHistory(items, "competency");
    expect(groups.map((g) => [g.title, g.items.length])).toEqual([
      ["系统设计", 2],
      ["LLM 应用", 1],
    ]);
  });

  it("搜索词都要命中，也搜要点和岗位名", () => {
    expect(groupHistory(items, "job", "令牌桶 限流").flatMap((g) => g.items)).toHaveLength(1);
    expect(groupHistory(items, "job", "ai").map((g) => g.title)).toEqual(["AI 工程师"]);
    expect(groupHistory(items, "job", "不存在的词")).toEqual([]);
  });

  it("没有岗位名时归到“未注明岗位”", () => {
    expect(groupHistory([item({ job_title: " " })], "job")[0]?.title).toBe("未注明岗位");
  });
});
