"use client";

import { buttonPrimary, buttonSecondary } from "@/lib/styles";
import { durationLabels, languageLabels, styleLabels, typeLabels } from "@/lib/labels";
import type { SetupSettings } from "@/lib/setup";

type Props = {
  value: SetupSettings;
  onChange: (value: SetupSettings) => void;
  onBack: () => void;
  onNext: () => void;
};

const TYPE_DESCRIPTIONS: Record<keyof typeof typeLabels, string> = {
  technical: "原理、项目里的设计取舍和动手能力",
  behavioral: "沟通协作、处理问题和做事方式，按 STAR 追问",
};

const STYLE_DESCRIPTIONS: Record<keyof typeof styleLabels, string> = {
  gentle: "语气温和，会引导你把思路讲出来",
  pressure: "追问更紧，专挑边界和细节",
};

const DURATION_HINTS: Record<keyof typeof durationLabels, string> = {
  15: "热身，题少一些",
  20: "各阶段都能走一遍",
  30: "每题都有时间深挖",
};

function Choice<T extends string | number>({
  name,
  legend,
  options,
  value,
  onSelect,
  columns = 1,
}: {
  name: string;
  legend: string;
  options: Array<{ value: T; label: string; hint?: string }>;
  value: T;
  onSelect: (value: T) => void;
  columns?: 1 | 2 | 3;
}) {
  const grid = { 1: "", 2: "sm:grid-cols-2", 3: "sm:grid-cols-3" }[columns];
  return (
    <fieldset className="flex min-w-0 flex-col gap-2">
      <legend className="mb-2 text-sm text-muted">{legend}</legend>
      <div className={`grid grid-cols-1 gap-x-6 border-t border-line ${grid}`}>
        {options.map((option) => {
          const checked = option.value === value;
          return (
            <label
              key={String(option.value)}
              className="group flex cursor-pointer items-start gap-3 border-b border-line py-3"
            >
              <input
                className="peer sr-only"
                type="radio"
                name={name}
                checked={checked}
                onChange={() => onSelect(option.value)}
              />
              <span
                aria-hidden="true"
                className={`mt-1.5 flex size-3.5 shrink-0 items-center justify-center rounded-full border peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-accent ${
                  checked ? "border-accent" : "border-faint group-hover:border-ink-soft"
                }`}
              >
                {checked && <span className="size-1.5 rounded-full bg-accent" />}
              </span>
              <span className="min-w-0">
                <span className={`block ${checked ? "text-ink" : "text-ink-soft"}`}>
                  {option.label}
                </span>
                {option.hint && (
                  <span className="block text-sm text-muted">{option.hint}</span>
                )}
              </span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

function entries<K extends string | number>(labels: Record<K, string>) {
  return Object.entries(labels) as Array<[K, string]>;
}

export default function SettingsStep({ value, onChange, onBack, onNext }: Props) {
  return (
    <section className="flex min-w-0 flex-col gap-8" aria-labelledby="settings-title">
      <div>
        <h2 className="text-2xl text-ink" id="settings-title">
          怎么面
        </h2>
        <p className="mt-1 text-sm text-muted">选面试类型、面试官风格、语言和时长。</p>
      </div>

      <div className="grid grid-cols-1 gap-8 sm:grid-cols-2 sm:gap-x-10">
        <Choice
          name="type"
          legend="面试类型"
          value={value.type}
          onSelect={(type) => onChange({ ...value, type })}
          options={entries(typeLabels).map(([key, label]) => ({
            value: key,
            label,
            hint: TYPE_DESCRIPTIONS[key],
          }))}
        />
        <Choice
          name="style"
          legend="面试官风格"
          value={value.style}
          onSelect={(style) => onChange({ ...value, style })}
          options={entries(styleLabels).map(([key, label]) => ({
            value: key,
            label,
            hint: STYLE_DESCRIPTIONS[key],
          }))}
        />
        <Choice
          name="language"
          legend="语言"
          value={value.language}
          onSelect={(language) => onChange({ ...value, language })}
          options={entries(languageLabels).map(([key, label]) => ({ value: key, label }))}
        />
        <Choice
          name="duration"
          legend="时长"
          value={value.duration_min}
          onSelect={(duration_min) => onChange({ ...value, duration_min })}
          options={([15, 20, 30] as const).map((minutes) => ({
            value: minutes,
            label: durationLabels[minutes],
            hint: DURATION_HINTS[minutes],
          }))}
        />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-6">
        <button className={buttonSecondary} type="button" onClick={onBack}>
          上一步
        </button>
        <button className={buttonPrimary} type="button" onClick={onNext}>
          下一步：生成计划
        </button>
      </div>
    </section>
  );
}
