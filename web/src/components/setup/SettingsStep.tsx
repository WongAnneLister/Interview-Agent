"use client";

import { buttonPrimary, buttonSecondary } from "@/lib/styles";
import {
  durationLabels,
  languageLabels,
  styleLabels,
  typeLabels,
} from "@/lib/labels";
import type { SetupSettings } from "@/lib/setup";

type Props = {
  value: SetupSettings;
  onChange: (value: SetupSettings) => void;
  onBack: () => void;
  onNext: () => void;
};

export default function SettingsStep({
  value,
  onChange,
  onBack,
  onNext,
}: Props) {
  function options<K extends Exclude<keyof SetupSettings, "duration_min">>(
    name: K,
    title: string,
    labels: Record<SetupSettings[K], string>,
  ) {
    return (
      <fieldset className="flex min-w-0 flex-col gap-4">
        <legend className="mb-2 font-semibold">{title}</legend>
        <div className="flex flex-wrap items-center gap-3">
          {(Object.entries(labels) as Array<[SetupSettings[K], string]>).map(
            ([option, label]) => (
              <label
                className="inline-flex cursor-pointer items-center gap-2 rounded-[9px] border border-border bg-white px-5 py-3 has-checked:border-accent has-checked:bg-[#eef7f1]"
                key={option}
              >
                <input
                  className="accent-accent"
                  type="radio"
                  name={name}
                  value={option}
                  checked={value[name] === option}
                  onChange={() => onChange({ ...value, [name]: option })}
                />{" "}
                {label}
              </label>
            ),
          )}
        </div>
      </fieldset>
    );
  }
  return (
    <section
      className="flex min-w-0 flex-col gap-4"
      aria-labelledby="settings-title"
    >
      <h2 className="text-[21px] font-[650]" id="settings-title">
        面试设置
      </h2>
      {options("type", "面试类型", typeLabels)}
      {options("style", "面试风格", styleLabels)}
      {options("language", "面试语言", languageLabels)}
      <fieldset className="flex min-w-0 flex-col gap-4">
        <legend className="mb-2 font-semibold">面试时长</legend>
        <div className="flex flex-wrap items-center gap-3">
          {([15, 20, 30] as const).map((duration) => (
            <label
              className="inline-flex cursor-pointer items-center gap-2 rounded-[9px] border border-border bg-white px-5 py-3 has-checked:border-accent has-checked:bg-[#eef7f1]"
              key={duration}
            >
              <input
                className="accent-accent"
                type="radio"
                name="duration"
                value={duration}
                checked={value.duration_min === duration}
                onChange={() => onChange({ ...value, duration_min: duration })}
              />{" "}
              {durationLabels[duration]}
            </label>
          ))}
        </div>
      </fieldset>
      <div className="flex flex-wrap items-center gap-3">
        <button className={buttonSecondary} type="button" onClick={onBack}>
          上一步：职位描述
        </button>
        <button className={buttonPrimary} type="button" onClick={onNext}>
          下一步：生成计划
        </button>
      </div>
    </section>
  );
}
