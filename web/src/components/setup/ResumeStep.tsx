"use client";

import { buttonPrimary, buttonSecondary, field, input } from "@/lib/styles";
import {
  savedResumeLabel,
  type ResumeDraft,
  type SavedResume,
} from "@/lib/setup";
import ResumeEditor from "./ResumeEditor";

type Props = {
  saved: SavedResume[] | null; // 还没加载或加载失败时为 null，不显示列表
  selectedId: string | null;
  notice: string | null;
  onPick: (id: string) => void;
  file: File | null;
  fileError: string | null;
  draft: ResumeDraft | null;
  errors: Record<string, string>;
  dirty: boolean;
  canContinue: boolean;
  onFileChange: (file: File | null) => void;
  onChange: (draft: ResumeDraft) => void;
  onUpload: () => void;
  onSave: () => void;
  onNext: () => void;
};

export default function ResumeStep(props: Props) {
  return (
    <section
      className="flex min-w-0 flex-col gap-4"
      aria-labelledby="resume-title"
    >
      <h2 className="text-[21px] font-[650]" id="resume-title">
        上传与确认简历
      </h2>
      {props.saved && props.saved.length > 0 && (
        <div className="flex min-w-0 flex-col gap-2">
          <h3 className="font-semibold">选用已保存的简历</h3>
          <p className="text-sm text-muted">
            直接使用之前的解析结果，不用等待，也不再花解析费用。
          </p>
          <ul className="flex min-w-0 flex-col gap-2">
            {props.saved.map((resume) => {
              const label = savedResumeLabel(resume);
              return (
                <li key={resume.id}>
                  <button
                    type="button"
                    className="w-full min-w-0 cursor-pointer rounded-[10px] border border-border bg-white px-4 py-3 text-left aria-pressed:border-accent aria-pressed:bg-accent/5 disabled:cursor-not-allowed disabled:opacity-60"
                    aria-pressed={resume.id === props.selectedId}
                    onClick={() => props.onPick(resume.id)}
                  >
                    <span className="block font-semibold">{label.title}</span>
                    <span className="block truncate text-sm text-muted">
                      {label.detail}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          <hr className="border-border" />
          <h3 className="font-semibold">或上传新简历</h3>
        </div>
      )}
      <p className="text-muted">
        上传可选中文字的 PDF，最大 10 MB。解析后可逐项修正。
      </p>
      <label className={field}>
        <span>PDF 简历</span>
        <input
          className={input}
          type="file"
          accept="application/pdf"
          aria-invalid={!!props.fileError}
          onChange={(event) =>
            props.onFileChange(event.target.files?.[0] ?? null)
          }
        />
      </label>
      {props.file && <p className="text-muted">已选择：{props.file.name}</p>}
      {props.fileError && (
        <p className="text-sm text-[#a32920]" role="alert">
          {props.fileError}
        </p>
      )}
      <div>
        <button
          type="button"
          className={buttonPrimary}
          disabled={!props.file || !!props.fileError}
          onClick={props.onUpload}
        >
          {props.draft ? "重新上传并解析" : "上传并解析"}
        </button>
      </div>
      {props.draft && (
        <>
          <hr className="border-border" />
          {props.notice && (
            <p className="text-sm text-accent" role="status">
              {props.notice}
            </p>
          )}
          <p className="text-muted">
            请确认解析结果。
            {props.dirty ? "有未保存修改，请先保存。" : "简历已保存。"}
          </p>
          <ResumeEditor
            value={props.draft}
            errors={props.errors}
            onChange={props.onChange}
          />
          <div className="flex flex-wrap items-center gap-3">
            <button
              className={buttonSecondary}
              type="button"
              disabled={!props.dirty}
              onClick={props.onSave}
            >
              保存修改
            </button>
            <button
              className={buttonPrimary}
              type="button"
              disabled={!props.canContinue}
              onClick={props.onNext}
            >
              下一步：职位描述
            </button>
          </div>
        </>
      )}
    </section>
  );
}
