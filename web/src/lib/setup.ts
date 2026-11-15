import type { components } from "./api/schema";
export type ResumeDraft = components["schemas"]["ResumeReplacement"];
export type SetupSettings = Pick<
  components["schemas"]["InterviewCreate"],
  "type" | "style" | "language" | "duration_min"
>;
export const defaultSettings: SetupSettings = {
  type: "technical",
  style: "gentle",
  language: "zh",
  duration_min: 20,
};
export const MAX_JD_CHARS = 10_000;

export function resumeToDraft(
  parsed: components["schemas"]["ResumeParsed"],
): ResumeDraft {
  return {
    name: parsed.name ?? "",
    work: parsed.work ?? [],
    projects: parsed.projects ?? [],
    skills: parsed.skills ?? [],
    education: parsed.education ?? [],
  };
}

export function validateResume(draft: ResumeDraft): Record<string, string> {
  const errors: Record<string, string> = {};
  draft.work.forEach((item, i) => {
    if (!item.company.trim()) errors[`work.${i}.company`] = "公司不能为空";
    if (!item.title.trim()) errors[`work.${i}.title`] = "职位不能为空";
  });
  draft.projects.forEach((item, i) => {
    if (!item.name.trim()) errors[`projects.${i}.name`] = "项目名称不能为空";
  });
  draft.education.forEach((item, i) => {
    if (!item.school.trim()) errors[`education.${i}.school`] = "学校不能为空";
  });
  return errors;
}

export function validateResumeFile(
  file: Pick<File, "name" | "type" | "size">,
): string | null {
  if (
    !/\.pdf$/i.test(file.name) ||
    (file.type !== "" && file.type !== "application/pdf")
  )
    return "请选择 PDF 格式的简历";
  if (file.size > 10 * 1024 * 1024) return "简历不能超过 10 MB";
  return null;
}

type Readiness = {
  resumeId: string | null;
  resumeDirty: boolean;
  jdText: string;
  previewText: string | null;
};
export function canAdvance(step: number, state: Readiness): boolean {
  const resumeReady = Boolean(state.resumeId) && !state.resumeDirty;
  if (step === 0) return resumeReady;
  return (
    resumeReady &&
    Boolean(state.jdText.trim()) &&
    [...state.jdText].length <= MAX_JD_CHARS &&
    state.previewText === state.jdText
  );
}

export function toInterviewCreate(
  resumeId: string,
  jdText: string,
  settings: SetupSettings,
): components["schemas"]["InterviewCreate"] {
  return { resume_id: resumeId, jd_text: jdText, ...settings };
}

export type SavedResume = components["schemas"]["ResumeSummary"];

/** 已保存简历在列表里的两行文字：文件名（认出是哪份），再是姓名、职位和最近修改日期。 */
export function savedResumeLabel(resume: SavedResume): {
  title: string;
  detail: string;
  named: boolean;
} {
  const named = Boolean(resume.file_name);
  const title = resume.file_name || "未记录文件名";
  const date = resume.updated_at.slice(0, 10);
  const detail = [resume.name.trim(), resume.headline, `${date} 更新`]
    .filter((part) => part)
    .join(" · ");
  return { title, detail, named };
}

export type SavedJd = components["schemas"]["SavedJDSummary"];

/** 已保存职位描述在列表里的两行文字：职位名称，再是正文开头和最近使用日期。 */
export function savedJdLabel(jd: SavedJd): { title: string; detail: string } {
  const firstLine =
    jd.text
      .split("\n")
      .map((line) => line.trim())
      .find((line) => line && line !== jd.title) ?? "";
  const snippet = [...firstLine].length > 40 ? `${[...firstLine].slice(0, 40).join("")}…` : firstLine;
  const detail = [snippet, `${jd.updated_at.slice(0, 10)} 使用`].filter(Boolean).join(" · ");
  return { title: jd.title || "未注明职位名称", detail };
}
