"use client";

import { buttonPrimary, buttonSecondary } from "@/lib/styles";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ErrorNotice } from "@/components/ErrorNotice";
import { LoadingNotice } from "@/components/LoadingNotice";
import { PracticeCalendar } from "@/components/PracticeCalendar";
import ResumeStep from "@/components/setup/ResumeStep";
import JdStep from "@/components/setup/JdStep";
import SettingsStep from "@/components/setup/SettingsStep";
import {
  createInterview,
  getResume,
  listResumes,
  parseJd,
  updateResume,
  uploadResume,
} from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";
import {
  durationLabels,
  languageLabels,
  styleLabels,
  typeLabels,
} from "@/lib/labels";
import {
  canAdvance,
  defaultSettings,
  resumeToDraft,
  toInterviewCreate,
  validateResume,
  validateResumeFile,
  type ResumeDraft,
  type SavedResume,
  type SetupSettings,
} from "@/lib/setup";
import { useRequest } from "@/lib/useRequest";

type Activity = "pick" | "upload" | "save" | "preview" | "create";
type ResumeOut = components["schemas"]["ResumeOut"];

const stepItems = [
  { id: 0, title: "简历", desc: "上传或选择简历" },
  { id: 1, title: "职位", desc: "解析目标岗位" },
  { id: 2, title: "设置", desc: "风格与时长设定" },
  { id: 3, title: "生成计划", desc: "出题与时间分配" },
];

const loadingMessages: Record<Activity, string> = {
  pick: "正在读取简历",
  upload: "正在解析简历，通常 10–30 秒",
  save: "正在保存修改",
  preview: "正在解析职位描述",
  create: "正在生成面试计划，通常 30–90 秒",
};

function friendlyError(
  error: ApiError | null,
  activity: Activity | null,
): ApiError | null {
  if (!error) return null;
  const messages: Record<string, string> = {
    file_too_large: "简历不能超过 10 MB，请选择较小的 PDF。",
    unsupported_media_type: "只接受 PDF 文件，请重新选择。",
    resume_no_text: "这份 PDF 没有可提取的文字，请上传可以选中文字的 PDF。",
    resume_unreadable: "无法读取这份 PDF，请确认文件没有损坏或加密。",
    resume_too_long: "简历文字超过 5 万字，请删减后再上传。",
    model_unavailable: "模型服务暂时不可用，请稍后重试。",
    user_not_configured: "尚未配置本地用户，请先运行 make seed。",
  };
  const message =
    error.code === "parse_failed" && activity === "upload"
      ? "简历解析失败，请检查文件或重试。"
      : (messages[error.code] ??
        (error.status === 413 ? messages.file_too_large : undefined));
  return message
    ? new ApiError(message, error.status, error.code, error.traceId)
    : error;
}

export default function NewInterviewPage() {
  const router = useRouter();
  const request = useRequest();
  const [step, setStep] = useState(0);
  const [furthestStep, setFurthestStep] = useState(0);
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [resumeId, setResumeId] = useState<string | null>(null);
  const [resume, setResume] = useState<ResumeDraft | null>(null);
  const [resumeDirty, setResumeDirty] = useState(false);
  const [resumeErrors, setResumeErrors] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<SavedResume[] | null>(null);
  const [pickId, setPickId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [jdText, setJdText] = useState("");
  const [preview, setPreview] = useState<
    components["schemas"]["JDParsed"] | null
  >(null);
  const [previewText, setPreviewText] = useState<string | null>(null);
  const [settings, setSettings] = useState<SetupSettings>(defaultSettings);
  const [activity, setActivity] = useState<Activity | null>(null);
  const [navigating, setNavigating] = useState(false);
  const locked = request.busy || navigating;
  const state = { resumeId, resumeDirty, jdText, previewText };

  function refreshSaved(signal?: AbortSignal) {
    listResumes(signal).then(setSaved, () => undefined);
  }

  useEffect(() => {
    const controller = new AbortController();
    refreshSaved(controller.signal);
    return () => controller.abort();
  }, []);

  function applyResume(result: ResumeOut, message: string | null) {
    setResumeId(result.id);
    setResume(resumeToDraft(result.parsed));
    setResumeDirty(false);
    setResumeErrors({});
    setNotice(message);
  }

  function goTo(next: number) {
    if (locked || (next > 0 && !canAdvance(next - 1, state))) return;
    request.clearError();
    setStep(next);
    setFurthestStep((previous) => Math.max(previous, next));
  }

  function selectFile(next: File | null) {
    request.clearError();
    setFile(next);
    setFileError(next ? validateResumeFile(next) : null);
  }

  function perform(next: Activity, id: string | null = pickId) {
    if (locked) return;
    if (next === "pick") {
      if (!id) return;
      setPickId(id);
      setActivity(next);
      void request.run(
        (signal) => getResume(id, signal),
        (result) =>
          applyResume(result, "已选用保存的简历，可以直接下一步，也可以修改后保存。"),
      );
    } else if (next === "upload") {
      if (!file) return;
      const invalid = validateResumeFile(file);
      setFileError(invalid);
      if (invalid) return;
      setActivity(next);
      void request.run(
        (signal) => uploadResume(file, signal),
        (result) => {
          applyResume(
            result,
            result.reused
              ? "这份文件之前上传过，已直接使用保存的解析结果，没有重新计费。"
              : null,
          );
          refreshSaved();
        },
      );
    } else if (next === "save") {
      if (!resume || !resumeId || !resumeDirty) return;
      const errors = validateResume(resume);
      setResumeErrors(errors);
      if (Object.keys(errors).length > 0) return;
      setActivity(next);
      void request.run(
        (signal) => updateResume(resumeId, resume, signal),
        (result) => {
          setResume(resumeToDraft(result.parsed));
          setResumeDirty(false);
          refreshSaved();
        },
      );
    } else if (next === "preview") {
      if (!jdText.trim() || [...jdText].length > 10000) return;
      const submittedText = jdText;
      setActivity(next);
      void request.run(
        (signal) => parseJd(submittedText, signal),
        (result) => {
          setPreview(result);
          setPreviewText(submittedText);
        },
      );
    } else {
      if (!resumeId || !canAdvance(3, state)) return;
      setActivity(next);
      void request.run(
        (signal) =>
          createInterview(
            toInterviewCreate(resumeId, jdText, settings),
            signal,
          ),
        (result) => {
          setNavigating(true);
          router.push(`/interviews/${result.id}/plan`);
        },
      );
    }
  }

  return (
    <div className="flex min-w-0 flex-col gap-8" aria-busy={locked}>
      <header className="flex flex-col gap-3">
        <h1 className="text-[32px] leading-tight text-ink sm:text-[38px]">
          准备一场面试
        </h1>
        <p className="text-muted">
          选好简历，贴上职位描述，定下时长和风格，系统会据此出一份面试计划。
        </p>
      </header>

      <PracticeCalendar />

      <nav aria-label="面试准备步骤">
        <ol className="flex flex-wrap gap-x-6 border-b border-line">
          {stepItems.map((item, index) => {
            const isCurrent = step === index;
            const isDisabled =
              locked ||
              index > furthestStep ||
              (index > 0 && !canAdvance(index - 1, state));
            return (
              <li key={item.id} className="shrink-0">
                <button
                  type="button"
                  aria-current={isCurrent ? "step" : undefined}
                  disabled={isDisabled}
                  onClick={() => goTo(index)}
                  className="-mb-px cursor-pointer border-b-2 border-transparent pb-3 text-sm text-muted transition-colors hover:text-ink aria-[current=step]:border-accent aria-[current=step]:text-ink disabled:cursor-not-allowed disabled:text-faint disabled:hover:text-faint"
                >
                  <span className="mr-1.5 font-serif text-faint">{index + 1}</span>
                  {item.title}
                </button>
              </li>
            );
          })}
        </ol>
      </nav>

      <section className="flex min-w-0 flex-col gap-6">
        {request.busy && activity && (
          <LoadingNotice message={loadingMessages[activity]} />
        )}
        {navigating && (
          <div className="flex items-center gap-3 rounded-lg bg-accent-wash border border-accent-line p-4 text-sm text-accent-ink" role="status">
            <span className="size-2 rounded-full bg-accent animate-ping" />
            面试计划已生成完毕，正在前往预览与调整页面…
          </div>
        )}
        <ErrorNotice
          error={friendlyError(request.error, activity)}
          onRetry={activity ? () => perform(activity) : undefined}
        />

        <fieldset disabled={locked} className="flex min-w-0 flex-col gap-4">
          <legend className="sr-only">{stepItems[step]?.title ?? "步骤"}</legend>

          {/* 步骤 1: 简历 */}
          {step === 0 && (
            <ResumeStep
              saved={saved}
              selectedId={resumeId}
              notice={notice}
              onPick={(id) => perform("pick", id)}
              file={file}
              fileError={fileError}
              draft={resume}
              errors={resumeErrors}
              dirty={resumeDirty}
              canContinue={canAdvance(0, state)}
              onFileChange={selectFile}
              onUpload={() => perform("upload")}
              onSave={() => perform("save")}
              onNext={() => goTo(1)}
              onChange={(draft) => {
                setResume(draft);
                setResumeDirty(true);
                setNotice(null);
                setResumeErrors({});
                request.clearError();
              }}
            />
          )}

          {/* 步骤 2: 职位描述 */}
          {step === 1 && (
            <JdStep
              text={jdText}
              preview={preview}
              previewText={previewText}
              canContinue={canAdvance(1, state)}
              onChange={(text) => {
                setJdText(text);
                request.clearError();
              }}
              onPreview={() => perform("preview")}
              onBack={() => goTo(0)}
              onNext={() => goTo(2)}
            />
          )}

          {/* 步骤 3: 面试设置 */}
          {step === 2 && (
            <SettingsStep
              value={settings}
              onChange={setSettings}
              onBack={() => goTo(1)}
              onNext={() => goTo(3)}
            />
          )}

          {/* 步骤 4: 确认与生成 */}
          {step === 3 && (
            <section
              className="flex min-w-0 flex-col gap-6"
              aria-labelledby="generate-title"
            >
              <div>
                <h2 className="text-2xl text-ink" id="generate-title">
                  确认后生成计划
                </h2>
                <p className="mt-1 text-sm text-muted">
                  生成后可以先看题目、增删修改，确认了再开始面试。
                </p>
              </div>

              <dl className="grid grid-cols-1 border-t border-line sm:grid-cols-2 sm:gap-x-10">
                {[
                  ["候选人", resume?.name || "未注明"],
                  ["目标岗位", preview?.title || "未注明"],
                  ["面试类型", typeLabels[settings.type]],
                  ["面试官风格", styleLabels[settings.style]],
                  ["语言", languageLabels[settings.language]],
                  ["时长", durationLabels[settings.duration_min]],
                ].map(([label, value]) => (
                  <div
                    key={label}
                    className="flex items-baseline justify-between gap-4 border-b border-line py-3"
                  >
                    <dt className="shrink-0 text-sm text-muted">{label}</dt>
                    <dd className="min-w-0 truncate text-right text-ink">{value}</dd>
                  </div>
                ))}
              </dl>

              {/* 底部按钮 */}
              <div className="flex flex-wrap items-center justify-between gap-3">
                <button
                  type="button"
                  className={buttonSecondary}
                  onClick={() => goTo(2)}
                >
                  上一步
                </button>
                <button
                  type="button"
                  className={buttonPrimary}
                  disabled={!canAdvance(3, state)}
                  onClick={() => perform("create")}
                >
                  生成面试计划
                </button>
              </div>
            </section>
          )}
        </fieldset>
      </section>
    </div>
  );
}
