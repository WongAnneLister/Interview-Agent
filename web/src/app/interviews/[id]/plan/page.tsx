"use client";

import { buttonPrimary, buttonSecondary, panel } from "@/lib/styles";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ErrorNotice } from "@/components/ErrorNotice";
import { LoadingNotice } from "@/components/LoadingNotice";
import { PlanView } from "@/components/plan/PlanView";
import { getPlan, updatePlan } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";
import { toPlanUpdate, validateQuestions } from "@/lib/plan";
import { useRequest } from "@/lib/useRequest";
import { IconArrowLeft } from "@/components/icons";

type Plan = components["schemas"]["PlanOut"];

export default function PlanPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [server, setServer] = useState<{ id: string; plan: Plan } | null>(null);
  const [questions, setQuestions] = useState<Plan["questions"]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [lockedId, setLockedId] = useState<string | null>(null);
  const [validation, setValidation] = useState<string[]>([]);
  const [saved, setSaved] = useState(false);
  const { run, busy, error, cancel, clearError } = useRequest();
  const plan = server?.id === id ? server.plan : null;
  const readOnly = lockedId === id || error?.status === 409;
  const dirty = Boolean(
    plan && JSON.stringify(plan.questions) !== JSON.stringify(questions),
  );

  function accept(value: Plan) {
    setServer({ id, plan: value });
    setQuestions(value.questions);
    setValidation([]);
  }

  useEffect(() => {
    void run(
      (signal) => getPlan(id, signal),
      (value) => {
        setServer({ id, plan: value });
        setQuestions(value.questions);
        setExpanded(new Set());
      },
    );
    return cancel;
  }, [id, run, cancel]);

  useEffect(() => {
    if (!dirty || readOnly) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty, readOnly]);

  function load() {
    void run((signal) => getPlan(id, signal), accept);
  }

  function save() {
    const issues = validateQuestions(questions);
    setValidation(issues);
    if (issues.length) return;
    setSaved(false);
    void run(
      async (signal) => {
        try {
          return await updatePlan(
            id,
            toPlanUpdate(questions).questions,
            signal,
          );
        } catch (cause) {
          if (cause instanceof ApiError && cause.status === 409) {
            setLockedId(id);
            if (plan) setQuestions(plan.questions);
          }
          throw cause;
        }
      },
      (value) => {
        accept(value);
        setSaved(true);
      },
    );
  }

  const shownError =
    error?.status === 404
      ? new ApiError("面试不存在，请确认链接是否有效", 404, error.code, error.traceId)
      : error?.status === 409
        ? new ApiError(
            "面试已开始，计划不能修改",
            409,
            error.code,
            error.traceId,
          )
        : error;

  return (
    <div className="flex min-w-0 flex-col gap-10">
      <header className="flex flex-col gap-3">
        <h1 className="text-[32px] leading-tight text-ink sm:text-[38px]">
          这一场练什么
        </h1>
        <p className="text-muted">
          先看看时间怎么分、考哪些能力。可以直接开始，也可以先改改题目。
        </p>
      </header>

      <ErrorNotice
        error={shownError}
        onRetry={
          error && error.status !== 404 && error.status !== 409
            ? plan
              ? save
              : load
            : undefined
        }
      />

      {busy && (
        <LoadingNotice message={plan ? "正在保存计划修改…" : "正在加载面试计划…"} />
      )}

      {!plan && !busy && !error && (
        <div className={`${panel} text-center py-12 text-faint`} role="status">
          正在加载面试计划中，请稍候…
        </div>
      )}

      {error?.status === 404 && (
        <div>
          <Link className={buttonSecondary} href="/new">
            <IconArrowLeft className="size-4" />
            返回准备页
          </Link>
        </div>
      )}

      {plan && (
        <>
          <PlanView
            plan={plan}
            questions={questions}
            expanded={expanded}
            readOnly={readOnly}
            busy={busy}
            onExpanded={setExpanded}
            onQuestions={(value) => {
              setQuestions(value);
              setSaved(false);
              setValidation([]);
            }}
          />

          {/* 启动与保存底栏 */}
          <section className="flex min-w-0 flex-col gap-5 border-t-2 border-ink pt-6">
            <h2 className="text-2xl text-ink">开始面试</h2>
            <p className="-mt-3 text-sm text-muted">
              语音更接近真实面试，文字适合不方便说话的时候。两种结束后都有带原话依据的报告。
            </p>

            {readOnly && (
              <p className="border-l-2 border-line pl-3 text-sm text-muted" role="status">
                这场面试已经开始，计划不能再改。
              </p>
            )}

            {dirty && !readOnly && (
              <p className="border-l-2 border-warn pl-3 text-sm text-warn-ink" role="status">
                题目有修改还没保存。先保存或放弃修改，再开始面试。
              </p>
            )}

            {saved && !dirty && (
              <p className="border-l-2 border-accent pl-3 text-sm text-accent-ink" role="status">
                修改已保存。
              </p>
            )}

            {validation.length > 0 && (
              <div role="alert" className="border-l-2 border-danger pl-3">
                <p className="text-sm text-danger-ink">这些地方要改一下才能保存：</p>
                <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-danger">
                  {validation.map((message) => (
                    <li key={message}>{message}</li>
                  ))}
                </ul>
              </div>
            )}

            <div className="flex flex-wrap items-center justify-between gap-4 pt-2">
              {!readOnly ? (
                <div className="flex flex-wrap items-center gap-2.5">
                  <button
                    type="button"
                    className={buttonSecondary}
                    disabled={!dirty || busy}
                    onClick={save}
                  >
                    保存修改
                  </button>
                  <button
                    type="button"
                    className="cursor-pointer px-2 text-sm text-muted underline-offset-4 hover:text-ink hover:underline disabled:cursor-not-allowed disabled:opacity-45 disabled:no-underline"
                    disabled={!dirty || busy}
                    onClick={() => {
                      setQuestions(plan.questions);
                      setValidation([]);
                      clearError();
                      setSaved(false);
                    }}
                  >
                    放弃修改
                  </button>
                </div>
              ) : <div />}

              <div className="flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  className={buttonSecondary}
                  disabled={(dirty && !readOnly) || busy}
                  onClick={() => router.push(`/interviews/${id}/text`)}
                >
                  文字面试
                </button>
                <button
                  type="button"
                  className={buttonPrimary}
                  disabled={(dirty && !readOnly) || busy}
                  onClick={() => router.push(`/interviews/${id}/voice`)}
                >
                  开始语音面试
                </button>
              </div>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
