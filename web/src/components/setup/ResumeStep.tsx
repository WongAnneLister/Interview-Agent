"use client";

import { buttonPrimary, buttonSecondary } from "@/lib/styles";
import {
  savedResumeLabel,
  type ResumeDraft,
  type SavedResume,
} from "@/lib/setup";
import ResumeEditor from "./ResumeEditor";
import { useRef, useState } from "react";

type Props = {
  saved: SavedResume[] | null;
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
  const fileInput = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const hasSaved = Boolean(props.saved && props.saved.length > 0);

  return (
    <section className="flex min-w-0 flex-col gap-8" aria-labelledby="resume-title">
      <div>
        <h2 className="text-2xl text-ink" id="resume-title">
          用哪份简历
        </h2>
        <p className="mt-1 text-sm text-muted">
          {hasSaved
            ? "选一份之前保存的，或者上传新的 PDF。"
            : "上传可以选中文字的 PDF，最大 10 MB，解析后可以逐项修改。"}
        </p>
      </div>

      {hasSaved && (
        <ul className="border-t border-line">
          {props.saved?.map((resume) => {
            const label = savedResumeLabel(resume);
            const selected = resume.id === props.selectedId;
            return (
              <li key={resume.id} className="border-b border-line">
                <button
                  type="button"
                  aria-pressed={selected}
                  onClick={() => props.onPick(resume.id)}
                  className="group flex w-full min-w-0 cursor-pointer items-center gap-4 py-4 text-left disabled:cursor-not-allowed"
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-serif text-lg text-ink group-hover:text-accent-ink">
                      {label.title}
                    </span>
                    <span className="block truncate text-sm text-muted">
                      {label.detail}
                    </span>
                  </span>
                  <span
                    className={`shrink-0 text-sm ${selected ? "text-accent" : "text-faint group-hover:text-ink"}`}
                  >
                    {selected ? "已选" : "使用"}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}

      <div className="flex flex-col gap-3">
        {hasSaved && <p className="text-sm text-muted">或者上传新的简历</p>}
        <input
          ref={fileInput}
          type="file"
          accept="application/pdf"
          className="sr-only"
          tabIndex={-1}
          aria-hidden="true"
          onChange={(event) => props.onFileChange(event.target.files?.[0] ?? null)}
        />
        <button
          type="button"
          onClick={() => fileInput.current?.click()}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            props.onFileChange(event.dataTransfer.files[0] ?? null);
          }}
          className={`flex w-full cursor-pointer flex-col items-center gap-1 rounded-md border border-dashed px-6 py-8 text-center transition-colors disabled:cursor-not-allowed ${
            props.fileError
              ? "border-danger-line bg-danger-wash/40"
              : dragging
                ? "border-accent bg-accent-wash/50"
                : "border-line hover:border-faint hover:bg-wash/60"
          }`}
        >
          <span className="text-ink">
            {props.file ? props.file.name : "点击选择，或把 PDF 拖到这里"}
          </span>
          <span className="text-xs text-muted">
            {props.file
              ? `${(props.file.size / (1024 * 1024)).toFixed(2)} MB · 点击换一份`
              : "需要能选中文字的 PDF，最大 10 MB"}
          </span>
        </button>
        {props.fileError && (
          <p className="text-sm text-danger" role="alert">
            {props.fileError}
          </p>
        )}
        {props.file && !props.fileError && (
          <div>
            <button type="button" className={buttonPrimary} onClick={props.onUpload}>
              上传并解析
            </button>
          </div>
        )}
      </div>

      {props.draft && (
        <div className="flex flex-col gap-5 border-t border-line pt-8">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h2 className="text-2xl text-ink">核对解析结果</h2>
            <span className={`text-sm ${props.dirty ? "text-warn" : "text-muted"}`}>
              {props.dirty ? "有修改还没保存" : "已保存"}
            </span>
          </div>
          {props.notice && (
            <p className="border-l-2 border-accent pl-3 text-sm text-accent-ink" role="status">
              {props.notice}
            </p>
          )}
          <ResumeEditor value={props.draft} errors={props.errors} onChange={props.onChange} />
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-6">
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
              下一步：职位
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
