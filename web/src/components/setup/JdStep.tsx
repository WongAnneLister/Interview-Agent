"use client";

import {
  buttonPrimary,
  buttonSecondary,
  panel,
  field,
  input,
} from "@/lib/styles";
import type { components } from "@/lib/api/schema";

type Props = {
  text: string;
  preview: components["schemas"]["JDParsed"] | null;
  previewText: string | null;
  canContinue: boolean;
  onChange: (text: string) => void;
  onPreview: () => void;
  onBack: () => void;
  onNext: () => void;
};

export default function JdStep(props: Props) {
  const count = [...props.text].length;
  const valid = props.text.trim().length > 0 && count <= 10000;
  const stale = props.previewText !== null && props.previewText !== props.text;
  return (
    <section className="flex min-w-0 flex-col gap-4" aria-labelledby="jd-title">
      <h2 className="text-[21px] font-[650]" id="jd-title">
        粘贴职位描述
      </h2>
      <label className={field}>
        <span>职位描述原文</span>
        <textarea
          className={`${input} resize-y`}
          rows={10}
          value={props.text}
          aria-describedby="jd-count"
          aria-invalid={count > 10000}
          onChange={(event) => props.onChange(event.target.value)}
          placeholder="粘贴目标岗位的职责、任职要求和技术要求"
        />
      </label>
      <p
        id="jd-count"
        className={count > 10000 ? "text-sm text-[#a32920]" : "text-muted"}
      >
        {count} / 10000 字{count > 10000 ? "，请删减后再解析" : ""}
      </p>
      <div>
        <button
          className={buttonPrimary}
          type="button"
          disabled={!valid}
          onClick={props.onPreview}
        >
          解析预览
        </button>
      </div>
      {stale && (
        <p className="text-sm text-[#a32920]" role="status">
          文本已修改，请重新解析。
        </p>
      )}
      {props.preview ? (
        <section
          className={`${panel} flex min-w-0 flex-col gap-4`}
          aria-label="职位解析预览"
        >
          <h3 className="text-base font-[650]">
            {props.preview.title || "未注明"}
          </h3>
          <p>级别：{props.preview.level || "未注明"}</p>
          {props.preview.competencies.map((item, index) => (
            <article
              className={`${panel} flex min-w-0 flex-col gap-4`}
              key={index}
            >
              <h4>
                {item.name}{" "}
                <span className="inline-block rounded-md bg-[#edf4ee] px-2.5 py-[3px] text-[13px] text-accent">
                  权重 {Math.round(item.weight * 100)}%
                </span>
              </h4>
              <p>{item.description}</p>
              <ul className="list-disc pl-[22px]">
                {item.probes.map((probe, i) => (
                  <li key={i}>{probe}</li>
                ))}
              </ul>
            </article>
          ))}
          <div
            className="flex flex-wrap items-center gap-3"
            aria-label="技术术语"
          >
            {props.preview.tech_terms.map((term) => (
              <span
                className="inline-block rounded-md bg-[#edf4ee] px-2.5 py-[3px] text-[13px] text-accent"
                key={term}
              >
                {term}
              </span>
            ))}
          </div>
        </section>
      ) : (
        <p className="text-muted">
          解析后，这里会显示岗位名称、能力项和技术术语。请先完成一次预览。
        </p>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <button
          className={buttonSecondary}
          type="button"
          onClick={props.onBack}
        >
          上一步：简历
        </button>
        <button
          className={buttonPrimary}
          type="button"
          disabled={!props.canContinue}
          onClick={props.onNext}
        >
          下一步：面试设置
        </button>
      </div>
    </section>
  );
}
