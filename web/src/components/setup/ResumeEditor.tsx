"use client";

import {
  buttonSecondary,
  buttonDanger,
  panel,
  field,
  input,
} from "@/lib/styles";
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
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  error?: string;
  multiline?: boolean;
}) {
  return (
    <label className={field}>
      <span>{label}</span>
      {multiline ? (
        <textarea
          className={`${input} resize-y`}
          rows={3}
          value={value}
          onChange={(event) => onChange(event.target.value)}
        />
      ) : (
        <input
          className={input}
          value={value}
          aria-invalid={!!error}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
      {error && (
        <span className="text-sm text-[#a32920]" role="alert">
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
    <div className="flex min-w-0 flex-col gap-4">
      <TextField
        label="姓名"
        value={value.name}
        onChange={(name) => update("name", name)}
      />
      <section
        className="flex min-w-0 flex-col gap-4"
        aria-labelledby="work-heading"
      >
        <h3 className="text-base font-[650]" id="work-heading">
          工作经历
        </h3>
        {value.work.length === 0 && (
          <p className="text-muted">未识别到工作经历，可手动添加。</p>
        )}
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
              className={`${panel} flex min-w-0 flex-col gap-4`}
              key={index}
            >
              <legend className="mb-2 font-semibold">
                工作经历 {index + 1}
              </legend>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <TextField
                  label="公司（必填）"
                  value={item.company}
                  error={errors[`work.${index}.company`]}
                  onChange={(company) => change({ company })}
                />
                <TextField
                  label="职位（必填）"
                  value={item.title}
                  error={errors[`work.${index}.title`]}
                  onChange={(title) => change({ title })}
                />
                <TextField
                  label="开始时间"
                  value={item.start ?? ""}
                  onChange={(start) => change({ start })}
                />
                <TextField
                  label="结束时间"
                  value={item.end ?? ""}
                  onChange={(end) => change({ end })}
                />
              </div>
              <TextField
                label="工作要点（每行一条）"
                multiline
                value={(item.highlights ?? []).join("\n")}
                onChange={(text) => change({ highlights: text.split("\n") })}
              />
              <button
                className={buttonDanger}
                type="button"
                onClick={() =>
                  update(
                    "work",
                    value.work.filter((_, i) => i !== index),
                  )
                }
              >
                删除工作经历 {index + 1}
              </button>
            </fieldset>
          );
        })}
        <div>
          <button
            className={buttonSecondary}
            type="button"
            onClick={() =>
              update("work", [
                ...value.work,
                { company: "", title: "", start: "", end: "", highlights: [] },
              ])
            }
          >
            添加工作经历
          </button>
        </div>
      </section>

      <section
        className="flex min-w-0 flex-col gap-4"
        aria-labelledby="projects-heading"
      >
        <h3 className="text-base font-[650]" id="projects-heading">
          项目经历
        </h3>
        {value.projects.length === 0 && (
          <p className="text-muted">未识别到项目经历，可手动添加。</p>
        )}
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
              className={`${panel} flex min-w-0 flex-col gap-4`}
              key={index}
            >
              <legend className="mb-2 font-semibold">
                项目经历 {index + 1}
              </legend>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <TextField
                  label="项目名称（必填）"
                  value={item.name}
                  error={errors[`projects.${index}.name`]}
                  onChange={(name) => change({ name })}
                />
                <TextField
                  label="本人角色"
                  value={item.role ?? ""}
                  onChange={(role) => change({ role })}
                />
              </div>
              <TextField
                label="技术栈（每行一项）"
                multiline
                value={(item.tech_stack ?? []).join("\n")}
                onChange={(text) => change({ tech_stack: text.split("\n") })}
              />
              <TextField
                label="项目亮点（每行一条）"
                multiline
                value={(item.highlights ?? []).join("\n")}
                onChange={(text) => change({ highlights: text.split("\n") })}
              />
              <button
                className={buttonDanger}
                type="button"
                onClick={() =>
                  update(
                    "projects",
                    value.projects.filter((_, i) => i !== index),
                  )
                }
              >
                删除项目经历 {index + 1}
              </button>
            </fieldset>
          );
        })}
        <div>
          <button
            className={buttonSecondary}
            type="button"
            onClick={() =>
              update("projects", [
                ...value.projects,
                { name: "", role: "", tech_stack: [], highlights: [] },
              ])
            }
          >
            添加项目经历
          </button>
        </div>
      </section>

      <section
        className="flex min-w-0 flex-col gap-4"
        aria-labelledby="skills-heading"
      >
        <h3 className="text-base font-[650]" id="skills-heading">
          技能
        </h3>
        {value.skills.length === 0 && (
          <p className="text-muted">未识别到技能，可手动补充。</p>
        )}
        <TextField
          label="技能（每行一项）"
          multiline
          value={value.skills.join("\n")}
          onChange={(text) => update("skills", text.split("\n"))}
        />
      </section>

      <section
        className="flex min-w-0 flex-col gap-4"
        aria-labelledby="education-heading"
      >
        <h3 className="text-base font-[650]" id="education-heading">
          教育经历
        </h3>
        {value.education.length === 0 && (
          <p className="text-muted">未识别到教育经历，可手动添加。</p>
        )}
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
              className={`${panel} flex min-w-0 flex-col gap-4`}
              key={index}
            >
              <legend className="mb-2 font-semibold">
                教育经历 {index + 1}
              </legend>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <TextField
                  label="学校（必填）"
                  value={item.school}
                  error={errors[`education.${index}.school`]}
                  onChange={(school) => change({ school })}
                />
                <TextField
                  label="学历"
                  value={item.degree ?? ""}
                  onChange={(degree) => change({ degree })}
                />
                <TextField
                  label="专业"
                  value={item.field ?? ""}
                  onChange={(field) => change({ field })}
                />
                <TextField
                  label="开始时间"
                  value={item.start ?? ""}
                  onChange={(start) => change({ start })}
                />
                <TextField
                  label="结束时间"
                  value={item.end ?? ""}
                  onChange={(end) => change({ end })}
                />
              </div>
              <button
                className={buttonDanger}
                type="button"
                onClick={() =>
                  update(
                    "education",
                    value.education.filter((_, i) => i !== index),
                  )
                }
              >
                删除教育经历 {index + 1}
              </button>
            </fieldset>
          );
        })}
        <div>
          <button
            className={buttonSecondary}
            type="button"
            onClick={() =>
              update("education", [
                ...value.education,
                { school: "", degree: "", field: "", start: "", end: "" },
              ])
            }
          >
            添加教育经历
          </button>
        </div>
      </section>
    </div>
  );
}
