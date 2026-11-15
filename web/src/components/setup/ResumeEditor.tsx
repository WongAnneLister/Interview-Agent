"use client";

import { field, input } from "@/lib/styles";
import type { ResumeDraft } from "@/lib/setup";

type Props = {
  value: ResumeDraft;
  errors: Record<string, string>;
  onChange: (value: ResumeDraft) => void;
};

function TextField({
  label,
  value,
  onChange,
  error,
  multiline = false,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  error?: string;
  multiline?: boolean;
  placeholder?: string;
}) {
  return (
    <label className={field}>
      <span>{label}</span>
      {multiline ? (
        <textarea
          className={`${input} resize-y text-sm`}
          rows={3}
          value={value}
          placeholder={placeholder}
          onChange={(event) => onChange(event.target.value)}
        />
      ) : (
        <input
          className={input}
          value={value}
          placeholder={placeholder}
          aria-invalid={!!error}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
      {error && (
        <span className="text-xs font-medium text-danger" role="alert">
          {error}
        </span>
      )}
    </label>
  );
}

export default function ResumeEditor({ value, errors, onChange }: Props) {
  function update<K extends keyof ResumeDraft>(key: K, next: ResumeDraft[K]) {
    onChange({ ...value, [key]: next });
  }

  return (
    <div className="flex min-w-0 flex-col gap-6">
      {/* 候选人姓名 */}
      <div>
        <div className="mb-3 font-serif text-xl text-ink">
          <span>候选人姓名</span>
        </div>
        <div className="max-w-md">
          <TextField
            label="姓名"
            placeholder="例如：张三"
            value={value.name}
            onChange={(name) => update("name", name)}
          />
        </div>
      </div>

      {/* 工作经历 */}
      <section
        className="flex min-w-0 flex-col gap-3"
        aria-labelledby="work-heading"
      >
        <div className="flex items-center justify-between">
          <div className="font-serif text-xl text-ink" id="work-heading">
            <span>工作经历 ({value.work.length})</span>
          </div>
          <button
            className="cursor-pointer text-sm text-accent underline-offset-4 hover:underline"
            type="button"
            onClick={() =>
              update("work", [
                ...value.work,
                { company: "", title: "", start: "", end: "", highlights: [] },
              ])
            }
          >
            + 添加工作经历
          </button>
        </div>

        {value.work.length === 0 && (
          <div className="border-t border-line py-4 text-sm text-muted">
            未识别到工作经历，可点击上方按钮手动添加。
          </div>
        )}

        <div className="flex flex-col gap-4">
          {value.work.map((item, index) => {
            const change = (next: Partial<typeof item>) =>
              update(
                "work",
                value.work.map((row, i) =>
                  i === index ? { ...row, ...next } : row,
                ),
              );
            return (
              <fieldset
                className="flex min-w-0 flex-col gap-4 border-t border-line pt-5"
                key={index}
              >
                <div className="flex items-center justify-between">
                  <span className="font-serif text-faint">
                    经历 #{index + 1}
                  </span>
                  <button
                    className="cursor-pointer px-2 py-1 text-sm text-muted transition-colors hover:text-danger"
                    type="button"
                    onClick={() =>
                      update(
                        "work",
                        value.work.filter((_, i) => i !== index),
                      )
                    }
                  >
                    删除
                  </button>
                </div>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <TextField
                    label="公司（必填）"
                    placeholder="公司名称"
                    value={item.company}
                    error={errors[`work.${index}.company`]}
                    onChange={(company) => change({ company })}
                  />
                  <TextField
                    label="职位（必填）"
                    placeholder="担任职位"
                    value={item.title}
                    error={errors[`work.${index}.title`]}
                    onChange={(title) => change({ title })}
                  />
                  <TextField
                    label="开始时间"
                    placeholder="例如：2021.06"
                    value={item.start ?? ""}
                    onChange={(start) => change({ start })}
                  />
                  <TextField
                    label="结束时间"
                    placeholder="例如：2024.03 或 至今"
                    value={item.end ?? ""}
                    onChange={(end) => change({ end })}
                  />
                </div>
                <TextField
                  label="工作要点与职责（每行一条）"
                  placeholder="负责前端架构设计与性能优化..."
                  multiline
                  value={(item.highlights ?? []).join("\n")}
                  onChange={(text) => change({ highlights: text.split("\n") })}
                />
              </fieldset>
            );
          })}
        </div>
      </section>

      {/* 项目经历 */}
      <section
        className="flex min-w-0 flex-col gap-3"
        aria-labelledby="projects-heading"
      >
        <div className="flex items-center justify-between">
          <div className="font-serif text-xl text-ink" id="projects-heading">
            <span>核心项目经历 ({value.projects.length})</span>
          </div>
          <button
            className="cursor-pointer text-sm text-accent underline-offset-4 hover:underline"
            type="button"
            onClick={() =>
              update("projects", [
                ...value.projects,
                { name: "", role: "", tech_stack: [], highlights: [] },
              ])
            }
          >
            + 添加项目经历
          </button>
        </div>

        {value.projects.length === 0 && (
          <div className="border-t border-line py-4 text-sm text-muted">
            未识别到项目经历，可点击上方按钮手动补充。
          </div>
        )}

        <div className="flex flex-col gap-4">
          {value.projects.map((item, index) => {
            const change = (next: Partial<typeof item>) =>
              update(
                "projects",
                value.projects.map((row, i) =>
                  i === index ? { ...row, ...next } : row,
                ),
              );
            return (
              <fieldset
                className="flex min-w-0 flex-col gap-4 border-t border-line pt-5"
                key={index}
              >
                <div className="flex items-center justify-between">
                  <span className="font-serif text-faint">
                    项目 #{index + 1}
                  </span>
                  <button
                    className="cursor-pointer px-2 py-1 text-sm text-muted transition-colors hover:text-danger"
                    type="button"
                    onClick={() =>
                      update(
                        "projects",
                        value.projects.filter((_, i) => i !== index),
                      )
                    }
                  >
                    删除
                  </button>
                </div>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <TextField
                    label="项目名称（必填）"
                    placeholder="项目全称"
                    value={item.name}
                    error={errors[`projects.${index}.name`]}
                    onChange={(name) => change({ name })}
                  />
                  <TextField
                    label="本人角色"
                    placeholder="例如：核心开发 / 架构师"
                    value={item.role ?? ""}
                    onChange={(role) => change({ role })}
                  />
                </div>
                <TextField
                  label="技术栈（每行一项）"
                  placeholder="React&#10;TypeScript&#10;TailwindCSS"
                  multiline
                  value={(item.tech_stack ?? []).join("\n")}
                  onChange={(text) => change({ tech_stack: text.split("\n") })}
                />
                <TextField
                  label="项目亮点与突破（每行一条）"
                  placeholder="通过按需加载降低首屏加载时间 40%..."
                  multiline
                  value={(item.highlights ?? []).join("\n")}
                  onChange={(text) => change({ highlights: text.split("\n") })}
                />
              </fieldset>
            );
          })}
        </div>
      </section>

      {/* 专业技能 */}
      <section
        className="flex min-w-0 flex-col gap-3"
        aria-labelledby="skills-heading"
      >
        <div className="font-serif text-xl text-ink" id="skills-heading">
          <span>专业技能清单</span>
        </div>
        <div>
          <TextField
            label="技能项（每行一项，支持快捷编辑）"
            placeholder="Go / Python / React / Kubernetes..."
            multiline
            value={value.skills.join("\n")}
            onChange={(text) => update("skills", text.split("\n"))}
          />
          {value.skills.filter(Boolean).length > 0 && (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {value.skills.filter(Boolean).map((skill, i) => (
                <span
                  key={i}
                  className="inline-flex items-center rounded-sm border border-line px-2 py-0.5 text-sm text-ink-soft"
                >
                  {skill.trim()}
                </span>
              ))}
            </div>
          )}
        </div>
      </section>

      {/* 教育经历 */}
      <section
        className="flex min-w-0 flex-col gap-3"
        aria-labelledby="education-heading"
      >
        <div className="flex items-center justify-between">
          <div className="font-serif text-xl text-ink" id="education-heading">
            <span>教育背景 ({value.education.length})</span>
          </div>
          <button
            className="cursor-pointer text-sm text-accent underline-offset-4 hover:underline"
            type="button"
            onClick={() =>
              update("education", [
                ...value.education,
                { school: "", degree: "", field: "", start: "", end: "" },
              ])
            }
          >
            + 添加教育经历
          </button>
        </div>

        {value.education.length === 0 && (
          <div className="border-t border-line py-4 text-sm text-muted">
            未识别到教育经历，可点击上方按钮添加。
          </div>
        )}

        <div className="flex flex-col gap-4">
          {value.education.map((item, index) => {
            const change = (next: Partial<typeof item>) =>
              update(
                "education",
                value.education.map((row, i) =>
                  i === index ? { ...row, ...next } : row,
                ),
              );
            return (
              <fieldset
                className="flex min-w-0 flex-col gap-4 border-t border-line pt-5"
                key={index}
              >
                <div className="flex items-center justify-between">
                  <span className="font-serif text-faint">
                    学历 #{index + 1}
                  </span>
                  <button
                    className="cursor-pointer px-2 py-1 text-sm text-muted transition-colors hover:text-danger"
                    type="button"
                    onClick={() =>
                      update(
                        "education",
                        value.education.filter((_, i) => i !== index),
                      )
                    }
                  >
                    删除
                  </button>
                </div>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <TextField
                    label="学校（必填）"
                    placeholder="毕业院校"
                    value={item.school}
                    error={errors[`education.${index}.school`]}
                    onChange={(school) => change({ school })}
                  />
                  <TextField
                    label="学历"
                    placeholder="例如：本科 / 硕士"
                    value={item.degree ?? ""}
                    onChange={(degree) => change({ degree })}
                  />
                  <TextField
                    label="专业"
                    placeholder="例如：计算机科学与技术"
                    value={item.field ?? ""}
                    onChange={(field) => change({ field })}
                  />
                  <div className="grid grid-cols-2 gap-2">
                    <TextField
                      label="入学时间"
                      placeholder="2016.09"
                      value={item.start ?? ""}
                      onChange={(start) => change({ start })}
                    />
                    <TextField
                      label="毕业时间"
                      placeholder="2020.06"
                      value={item.end ?? ""}
                      onChange={(end) => change({ end })}
                    />
                  </div>
                </div>
              </fieldset>
            );
          })}
        </div>
      </section>
    </div>
  );
}
