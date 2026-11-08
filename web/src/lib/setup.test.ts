import { expect, it } from "vitest";
import {
  canAdvance,
  defaultSettings,
  resumeToDraft,
  toInterviewCreate,
  validateResume,
  validateResumeFile,
} from "./setup";
const ready = {
  resumeId: "resume-id",
  resumeDirty: false,
  jdText: "后端工程师",
  previewText: "后端工程师",
};
it("简历上传且保存后才能推进", () => {
  expect(canAdvance(0, { ...ready, resumeId: null })).toBe(false);
  expect(canAdvance(0, { ...ready, resumeDirty: true })).toBe(false);
  expect(canAdvance(0, ready)).toBe(true);
});
it.each(["", "  ", "x".repeat(10001), "修改后的 JD"])(
  "空白、超长和过期 JD 阻止推进",
  (jdText) => {
    expect(canAdvance(1, { ...ready, jdText })).toBe(false);
    expect(canAdvance(2, { ...ready, jdText })).toBe(false);
  },
);
it("有效预览和已保存简历允许进入生成", () => {
  expect(canAdvance(1, ready)).toBe(true);
  expect(canAdvance(2, ready)).toBe(true);
  expect(canAdvance(2, { ...ready, resumeDirty: true })).toBe(false);
  expect(canAdvance(1, { ...ready, previewText: null })).toBe(false);
});
it("将表单映射为后端请求，不包含预览结果", () => {
  expect(toInterviewCreate("id", " JD ", defaultSettings)).toEqual({
    resume_id: "id",
    jd_text: " JD ",
    type: "technical",
    style: "gentle",
    language: "zh",
    duration_min: 20,
  });
});
it("解析字段的缺省值转成整份替换请求", () => {
  expect(resumeToDraft({ name: "" })).toEqual({
    name: "",
    work: [],
    projects: [],
    skills: [],
    education: [],
  });
});
it("简历非空字段错误定位到具体列表项", () => {
  expect(
    validateResume({
      ...resumeToDraft({ name: "" }),
      work: [{ company: " ", title: "", start: "", end: "" }],
      projects: [{ name: "", role: "" }],
      education: [{ school: "", degree: "", field: "", start: "", end: "" }],
    }),
  ).toEqual({
    "work.0.company": "公司不能为空",
    "work.0.title": "职位不能为空",
    "projects.0.name": "项目名称不能为空",
    "education.0.school": "学校不能为空",
  });
});
it("在请求前拒绝非 PDF 和超大文件", () => {
  expect(
    validateResumeFile({ name: "cv.txt", type: "text/plain", size: 1 }),
  ).toContain("PDF");
  expect(
    validateResumeFile({ name: "cv.pdf", type: "text/plain", size: 1 }),
  ).toContain("PDF");
  expect(
    validateResumeFile({ name: "cv.PDF", type: "", size: 10 * 1024 * 1024 }),
  ).toBeNull();
  expect(
    validateResumeFile({
      name: "cv.pdf",
      type: "application/pdf",
      size: 10 * 1024 * 1024 + 1,
    }),
  ).toContain("10 MB");
});
