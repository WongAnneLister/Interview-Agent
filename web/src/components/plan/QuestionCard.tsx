"use client";

import { field, input } from "@/lib/styles";
import type { components } from "@/lib/api/schema";
import { IconChevronDown, IconChevronUp } from "@/components/icons";

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
    <fieldset className="flex min-w-0 flex-col gap-3" disabled={disabled}>
      <legend className="mb-2 text-xs text-muted">{label}</legend>
      <div className="flex flex-col gap-3">
        {values.map((value, index) => (
          <div
            key={index}
            className="flex flex-col items-stretch gap-2 sm:flex-row sm:items-end"
          >
            <label className={`${field} flex-1`}>
              <span className="sr-only">
                {label} {index + 1}
              </span>
              <textarea
                className={`${input} resize-y text-sm`}
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
              className="cursor-pointer px-2 py-2 text-sm text-muted transition-colors hover:text-danger disabled:pointer-events-none disabled:opacity-40"
              disabled={values.length <= min}
              onClick={() => onChange(values.filter((_, i) => i !== index))}
            >
              删除
            </button>
          </div>
        ))}
      </div>
      <div>
        <button
          type="button"
          className="cursor-pointer text-sm text-accent underline-offset-4 hover:underline disabled:pointer-events-none disabled:opacity-40"
          disabled={max !== undefined && values.length >= max}
          onClick={() => onChange([...values, ""])}
        >
          + 添加{label}
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
    <article className="flex min-w-0 flex-col gap-4 border-t border-line pt-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-1 items-start gap-3">
          <span className="w-6 shrink-0 font-serif text-lg leading-6 text-faint">
            {number}
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h4 className="text-ink">{question.competency}</h4>
              {question.project && (
                <span className="text-sm text-muted">· {question.project}</span>
              )}
            </div>
            {!expanded && (
              <p className="mt-1 text-sm text-muted line-clamp-1">
                {question.text}
              </p>
            )}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {!readOnly && (
            <button
              className="cursor-pointer px-2 py-1 text-sm text-muted transition-colors hover:text-danger disabled:cursor-not-allowed"
              disabled={busy}
              onClick={onDelete}
              title={`删除第 ${number} 题`}
            >
              删除
            </button>
          )}
          <button
            className="inline-flex cursor-pointer items-center gap-1 px-2 py-1 text-sm text-muted transition-colors hover:text-ink"
            aria-expanded={expanded}
            onClick={onToggle}
          >
            {expanded ? (
              <>
                <span>收起</span>
                <IconChevronUp className="size-3.5" />
              </>
            ) : (
              <>
                <span>展开</span>
                <IconChevronDown className="size-3.5" />
              </>
            )}
          </button>
        </div>
      </div>

      {expanded && (
        <div className="flex min-w-0 flex-col gap-5 pl-9">
          <label className={field}>
            <span>题目</span>
            <textarea
              className={`${input} resize-y text-sm leading-relaxed`}
              rows={3}
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
            label="追问方向"
            values={question.follow_ups}
            min={2}
            max={3}
            disabled={readOnly || busy}
            onChange={(values) => onChange({ ...question, follow_ups: values })}
          />
        </div>
      )}
    </article>
  );
}
