"use client";

import { buttonPrimary, buttonSecondary, panel } from "@/lib/styles";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { ErrorNotice } from "@/components/ErrorNotice";
import { LoadingNotice } from "@/components/LoadingNotice";
import ResumeStep from "@/components/setup/ResumeStep";
import JdStep from "@/components/setup/JdStep";
import SettingsStep from "@/components/setup/SettingsStep";
import {
  createInterview,
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
  type SetupSettings,
} from "@/lib/setup";
import { useRequest } from "@/lib/useRequest";

type Activity = "upload" | "save" | "preview" | "create";
const steps = ["简历", "职位描述", "面试设置", "生成计划"];
const loadingMessages: Record<Activity, string> = {
  upload: "正在解析简历（通常需要 10–30 秒）",
  save: "正在保存简历修改",
  preview: "正在解析职位描述，请稍候",
  create: "正在生成面试计划（通常需要 30–90 秒，请不要关闭页面）",
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
      ? "简历解析失败，可重试。"
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

  function perform(next: Activity) {
    if (locked) return;
    if (next === "upload") {
      if (!file) return;
      const invalid = validateResumeFile(file);
      setFileError(invalid);
      if (invalid) return;
      setActivity(next);
      void request.run(
        (signal) => uploadResume(file, signal),
        (result) => {
          setResumeId(result.id);
          setResume(resumeToDraft(result.parsed));
          setResumeDirty(false);
          setResumeErrors({});
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
        },
      );
    } else if (next === "preview") {
      if (!jdText.trim() || [...jdText].length > 10000) return;
      // Associate the response with the submitted text, even if the input later changes.
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
    <div className="flex min-w-0 flex-col gap-4" aria-busy={locked}>
      <header className="flex min-w-0 flex-col gap-4">
        <p className="text-xs font-bold tracking-[0.12em] text-accent">
          开始一场有针对性的练习
        </p>
        <h1 className="text-[clamp(28px,4vw,38px)] leading-[1.35] font-bold tracking-[-0.04em]">
          准备模拟面试
        </h1>
        <p className="text-muted">确认简历和岗位要求，生成属于你的面试计划。</p>
      </header>
      <nav aria-label="面试准备步骤">
        <ol className="grid grid-cols-4 gap-1 sm:gap-2">
          {steps.map((title, index) => (
            <li key={title}>
              <button
                className="cursor-pointer rounded-[10px] border border-border bg-white px-1 py-3 text-xs text-muted aria-[current=step]:border-accent aria-[current=step]:bg-accent aria-[current=step]:text-white disabled:cursor-not-allowed disabled:opacity-45 sm:px-2 sm:py-3.5 sm:text-[15px]"
                type="button"
                aria-current={step === index ? "step" : undefined}
                disabled={
                  locked ||
                  index > furthestStep ||
                  (index > 0 && !canAdvance(index - 1, state))
                }
                onClick={() => goTo(index)}
              >
                <span>{index + 1}</span> {title}
              </button>
            </li>
          ))}
        </ol>
      </nav>
      <section className={`${panel} flex min-w-0 flex-col gap-4`}>
        {request.busy && activity && (
          <LoadingNotice message={loadingMessages[activity]} />
        )}
        {navigating && <p role="status">计划已生成，正在打开预览…</p>}
        <ErrorNotice
          error={friendlyError(request.error, activity)}
          onRetry={activity ? () => perform(activity) : undefined}
        />
        <fieldset disabled={locked} className="flex min-w-0 flex-col gap-4">
          <legend className="sr-only">{steps[step]}</legend>
          {step === 0 && (
            <ResumeStep
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
                setResumeErrors({});
                request.clearError();
              }}
            />
          )}
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
          {step === 2 && (
            <SettingsStep
              value={settings}
              onChange={setSettings}
              onBack={() => goTo(1)}
              onNext={() => goTo(3)}
            />
          )}
          {step === 3 && (
            <section
              className="flex min-w-0 flex-col gap-4"
              aria-labelledby="generate-title"
            >
              <h2 className="text-[21px] font-[650]" id="generate-title">
                生成面试计划
              </h2>
              <p className="text-muted">
                确认以下信息。生成后可以预览与修改题目，再开始练习。
              </p>
              <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div>
                  <dt className="text-[13px] text-muted">候选人</dt>
                  <dd className="font-semibold">{resume?.name || "未注明"}</dd>
                </div>
                <div>
                  <dt className="text-[13px] text-muted">目标岗位</dt>
                  <dd className="font-semibold">
                    {preview?.title || "未注明"}
                  </dd>
                </div>
                <div>
                  <dt className="text-[13px] text-muted">面试类型</dt>
                  <dd className="font-semibold">{typeLabels[settings.type]}</dd>
                </div>
                <div>
                  <dt className="text-[13px] text-muted">面试风格</dt>
                  <dd className="font-semibold">
                    {styleLabels[settings.style]}
                  </dd>
                </div>
                <div>
                  <dt className="text-[13px] text-muted">面试语言</dt>
                  <dd className="font-semibold">
                    {languageLabels[settings.language]}
                  </dd>
                </div>
                <div>
                  <dt className="text-[13px] text-muted">面试时长</dt>
                  <dd className="font-semibold">
                    {durationLabels[settings.duration_min]}
                  </dd>
                </div>
              </dl>
              <div className="flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  className={buttonSecondary}
                  onClick={() => goTo(2)}
                >
                  上一步：面试设置
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
