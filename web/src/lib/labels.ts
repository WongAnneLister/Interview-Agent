import type { components } from "./api/schema";
type Schemas = components["schemas"];
export const typeLabels = {
  technical: "技术面",
  behavioral: "行为面",
} satisfies Record<Schemas["InterviewType"], string>;
export const styleLabels = {
  gentle: "温和",
  pressure: "压力",
} satisfies Record<Schemas["InterviewStyle"], string>;
export const languageLabels = { zh: "中文", en: "英文" } satisfies Record<
  Schemas["Language"],
  string
>;
export const durationLabels = {
  15: "15 分钟",
  20: "20 分钟",
  30: "30 分钟",
} satisfies Record<Schemas["InterviewCreate"]["duration_min"], string>;
export const stageLabels = {
  opening: "开场",
  self_intro: "自我介绍",
  resume_deep_dive: "简历深挖",
  technical: "技术问题",
  behavioral: "行为问题",
  candidate_questions: "候选人提问",
  closing: "收尾",
} satisfies Record<Schemas["Stage"], string>;
