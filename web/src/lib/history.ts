import type { components } from "./api/schema";
import { stageLabels } from "./labels";

export type HistoryQuestion = components["schemas"]["HistoryQuestion"];
export type GroupBy = "job" | "stage" | "competency";

export type HistoryGroup = {
  key: string;
  title: string;
  items: HistoryQuestion[];
};

const stageOrder = Object.keys(stageLabels);

function matches(item: HistoryQuestion, words: string[]): boolean {
  if (words.length === 0) return true;
  const q = item.question;
  const haystack = [
    q.text,
    q.competency,
    q.project ?? "",
    item.job_title,
    ...q.key_points,
    ...q.follow_ups,
  ]
    .join("\n")
    .toLowerCase();
  return words.every((word) => haystack.includes(word));
}

function groupKey(item: HistoryQuestion, by: GroupBy): [string, string] {
  if (by === "stage") {
    const stage = item.question.stage;
    return [stage, stageLabels[stage] ?? stage];
  }
  if (by === "competency") {
    const name = item.question.competency.trim();
    return [name, name || "未注明能力"];
  }
  const title = item.job_title.trim();
  return [title, title || "未注明岗位"];
}

/** 按岗位、题型或能力把历史题目分组；搜索词用空格分开，全部命中才保留。 */
export function groupHistory(
  items: HistoryQuestion[],
  by: GroupBy,
  query = "",
): HistoryGroup[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const groups = new Map<string, HistoryGroup>();
  const newest = new Map<string, string>();
  for (const item of items) {
    if (!matches(item, words)) continue;
    const [key, title] = groupKey(item, by);
    const group = groups.get(key) ?? { key, title, items: [] };
    group.items.push(item);
    groups.set(key, group);
    if ((newest.get(key) ?? "") < item.created_at) newest.set(key, item.created_at);
  }
  const result = [...groups.values()];
  for (const group of result) {
    group.items.sort((a, b) => b.created_at.localeCompare(a.created_at));
  }
  if (by === "stage") {
    return result.sort((a, b) => stageOrder.indexOf(a.key) - stageOrder.indexOf(b.key));
  }
  if (by === "competency") {
    return result.sort(
      (a, b) => b.items.length - a.items.length || a.title.localeCompare(b.title, "zh"),
    );
  }
  // 岗位：最近练过的在前
  return result.sort((a, b) =>
    (newest.get(b.key) ?? "").localeCompare(newest.get(a.key) ?? ""),
  );
}
