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
      ? new ApiError("面试不存在", 404, error.code, error.traceId)
      : error?.status === 409
        ? new ApiError(
            "面试已开始，计划不能修改",
            409,
            error.code,
            error.traceId,
          )
        : error;
  return (
    <div className="flex min-w-0 flex-col gap-7">
      <div className="flex min-w-0 flex-col gap-2.5">
        <span className="text-xs font-bold tracking-[0.12em] text-accent">
          准备就绪 · 计划预览
        </span>
        <h1 className="text-[clamp(28px,4vw,38px)] leading-[1.35] font-bold tracking-[-0.04em]">
          这一场，练什么？
        </h1>
        <p className="text-muted">先了解节奏，再带着真实的反应进入面试。</p>
      </div>
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
        <LoadingNotice message={plan ? "正在保存计划" : "正在加载面试计划"} />
      )}
      {!plan && !busy && !error && (
        <p className={`${panel} text-muted`} role="status">
          正在准备计划预览…
        </p>
      )}
      {error?.status === 404 && (
        <div>
          <Link className={buttonSecondary} href="/new">
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
          <section className={`${panel} flex min-w-0 flex-col gap-4`}>
            <h2 className="text-[21px] font-[650]">准备好开始了吗？</h2>
            {readOnly && (
              <p role="status">面试已开始，计划不能修改。当前为只读预览。</p>
            )}
            {dirty && !readOnly && (
              <p role="status">
                有未保存修改，请先保存或放弃修改，再开始面试。
              </p>
            )}
            {saved && !dirty && (
              <p role="status" className="text-accent">
                计划已保存
              </p>
            )}
            {validation.length > 0 && (
              <ul
                role="alert"
                className="list-disc pl-[22px] text-sm text-[#a32920]"
              >
                {validation.map((message) => (
                  <li key={message}>{message}</li>
                ))}
              </ul>
            )}
            <div className="flex flex-wrap items-center gap-3">
              {!readOnly && (
                <>
                  <button
                    className={buttonSecondary}
                    disabled={!dirty || busy}
                    onClick={save}
                  >
                    保存修改
                  </button>
                  <button
                    className={buttonSecondary}
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
                </>
              )}
              <button
                className={buttonPrimary}
                disabled={(dirty && !readOnly) || busy}
                onClick={() => router.push(`/interviews/${id}/text`)}
              >
                开始面试
              </button>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
