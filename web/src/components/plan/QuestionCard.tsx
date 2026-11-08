"use client";
import { buttonSecondary, buttonDanger, field, input } from "@/lib/styles";
import type { components } from "@/lib/api/schema";
type Question = components["schemas"]["Question"];
function Lines({
  label,
  values,
  min,
  max,
  disabled,
  onChange,
}: {
  label: string;
  values: string[];
  min: number;
  max?: number;
  disabled: boolean;
  onChange: (values: string[]) => void;
}) {
  return (
    <fieldset className="flex min-w-0 flex-col gap-4" disabled={disabled}>
      <legend className="mb-2 font-semibold">{label}</legend>
      {values.map((value, index) => (
        <div
          key={index}
          className="flex flex-col items-stretch gap-3 sm:flex-row sm:items-end"
        >
          <label className={`${field} flex-1`}>
            <span>
              {label} {index + 1}
            </span>
            <textarea
              className={`${input} resize-y`}
              value={value}
              rows={2}
              onChange={(e) =>
                onChange(
                  values.map((line, i) =>
                    i === index ? e.target.value : line,
                  ),
                )
              }
            />
          </label>
          <button
            type="button"
            className={buttonSecondary}
            disabled={values.length <= min}
            onClick={() => onChange(values.filter((_, i) => i !== index))}
          >
            删除{label} {index + 1}
          </button>
        </div>
      ))}
      <div>
        <button
          type="button"
          className={buttonSecondary}
          disabled={max !== undefined && values.length >= max}
          onClick={() => onChange([...values, ""])}
        >
          添加{label}
        </button>
      </div>
    </fieldset>
  );
}
export function QuestionCard({
  question,
  number,
  expanded,
  readOnly,
  busy,
  onToggle,
  onChange,
  onDelete,
}: {
  question: Question;
  number: number;
  expanded: boolean;
  readOnly: boolean;
  busy: boolean;
  onToggle: () => void;
  onChange: (question: Question) => void;
  onDelete: () => void;
}) {
  return (
    <article className="flex min-w-0 flex-col gap-4 rounded-xl border border-border bg-white p-5 sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-4 sm:flex-nowrap">
        <div>
          <h3 className="text-base font-[650]">
            第 {number} 题 · {question.competency}
          </h3>
          <p className="text-muted">关联项目：{question.project || "无"}</p>
        </div>
        <button
          className={buttonSecondary}
          aria-expanded={expanded}
          onClick={onToggle}
        >
          {expanded ? "收起" : "展开"}
        </button>
      </div>
      {expanded && (
        <div className="flex min-w-0 flex-col gap-4">
          <label className={field}>
            <span>题目正文</span>
            <textarea
              className={`${input} resize-y`}
              rows={4}
              disabled={readOnly || busy}
              value={question.text}
              onChange={(e) => onChange({ ...question, text: e.target.value })}
            />
          </label>
          <Lines
            label="考察要点"
            values={question.key_points}
            min={1}
            disabled={readOnly || busy}
            onChange={(values) => onChange({ ...question, key_points: values })}
          />
          <Lines
            label="追问"
            values={question.follow_ups}
            min={2}
            max={3}
            disabled={readOnly || busy}
            onChange={(values) => onChange({ ...question, follow_ups: values })}
          />
        </div>
      )}
      {!readOnly && (
        <div>
          <button className={buttonDanger} disabled={busy} onClick={onDelete}>
            删除第 {number} 题
          </button>
        </div>
      )}
    </article>
  );
}
