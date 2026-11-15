"use client";

import { useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { ErrorNotice } from "@/components/ErrorNotice";
import { phaseLabels } from "@/lib/labels";
import { controls } from "@/lib/interview/session";
import { shouldSubmit } from "@/lib/interview/composer";
import { useInterviewSession } from "@/lib/interview/useInterviewSession";
import { buttonPrimary, buttonSecondary, buttonDanger, input } from "@/lib/styles";

const activityLabels = {
  listening: "等待作答中",
  thinking: "面试官正在思考",
  speaking: "面试官正在发言",
  paused: "面试已暂停",
};

function time(seconds: number | null) {
  return seconds === null
    ? "--:--"
    : `${Math.floor(seconds / 60).toString().padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`;
}

export default function TextInterviewPage() {
  const { id } = useParams<{ id: string }>();
  return <InterviewSession key={id} id={id} />;
}

function InterviewSession({ id }: { id: string }) {
  const { state, attempt, pending, sendText, command, reconnect, clearError } =
    useInterviewSession(id);
  const [draft, setDraft] = useState("");
  const transcript = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  const confirmation = useRef<HTMLDialogElement>(null);
  const enabled = controls(state);
  const count = Array.from(draft).length;
  const canInput = enabled.input && !pending;

  const connectionLabel =
    state.connection === "connecting"
      ? "正在连接"
      : state.connection === "reconnecting"
        ? `重连中 (第 ${attempt} 次)`
        : state.connection === "failed"
          ? "已断开"
          : state.connection === "ended"
            ? "已结束"
            : "连接正常";

  const placeholder =
    state.connection !== "open"
      ? "网络恢复后即可作答…"
      : pending
        ? "正在发送，请稍候…"
        : state.activity === "paused"
          ? "面试已暂停，点击上方“继续”后即可作答…"
          : state.activity === "speaking"
            ? "面试官正在发言，稍后即可输入作答…"
            : state.activity === "thinking"
              ? "面试官正在评估上一个回答并准备问题…"
              : "输入你的回答，按 Enter 发送，Shift+Enter 换行…";

  useEffect(() => {
    if (nearBottom.current && transcript.current) {
      transcript.current.scrollTop = transcript.current.scrollHeight;
    }
  }, [state.messages]);

  function submit() {
    if (sendText(draft)) setDraft("");
  }

  return (
    <section className="flex min-w-0 flex-col gap-6">
      <header className="sticky top-0 z-30 -mx-4 border-b border-line bg-paper/95 px-4 py-3 backdrop-blur-sm sm:-mx-8 sm:px-8">
        <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 text-sm">
          <span className="font-serif text-lg text-ink">
            文字面试<span className="mx-2 text-faint">·</span>
            {state.phase ? phaseLabels[state.phase] : "准备中"}
          </span>
          <span className="flex flex-wrap items-baseline gap-x-4 text-muted">
            <span>
              本阶段 <span className="font-serif text-ink">{time(state.stageRemainingS)}</span>
            </span>
            <span>
              全场剩余 <span className="font-serif text-ink">{time(state.totalRemainingS)}</span>
            </span>
            <span className={state.activity === "thinking" ? "text-warn" : ""}>
              {state.activity ? activityLabels[state.activity] : "等待面试官"}
              <span className="text-faint"> · {connectionLabel}</span>
            </span>
          </span>
        </div>
      </header>

      {state.connection === "ended" && (
        <p className="border-l-2 border-accent pl-3 text-sm text-accent-ink" role="status">
          面试已结束，正在生成报告…
        </p>
      )}

      {state.error && (
        <div className="flex flex-col items-start gap-2">
          <ErrorNotice error={state.error} />
          <button className="text-sm text-muted underline-offset-4 hover:text-ink hover:underline cursor-pointer disabled:cursor-not-allowed disabled:text-faint disabled:no-underline" onClick={clearError}>
            关闭提示
          </button>
        </div>
      )}

      {state.connection === "failed" && (
        <div className="flex flex-wrap items-center justify-between gap-3 border-l-2 border-danger pl-3">
          <p className="text-sm text-danger-ink">和服务器的连接断了，检查网络后重新连接。</p>
          <button className={buttonSecondary} onClick={reconnect}>
            重新连接
          </button>
        </div>
      )}

      <div
        ref={transcript}
        role="log"
        aria-label="面试对话"
        aria-live="polite"
        aria-relevant="additions text"
        onScroll={() => {
          const el = transcript.current;
          if (el) {
            nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 96;
          }
        }}
        className="flex h-[52dvh] min-h-64 flex-col overflow-y-auto overscroll-contain border-y border-line"
      >
        {state.resumed && (
          <p className="border-b border-line py-3 text-center text-sm text-muted">
            已回到之前的面试。之前的对话没有显示出来，可以点“再说一遍题目”重听当前问题。
          </p>
        )}

        {!state.messages.length && !state.resumed && (
          <p className="m-auto py-12 text-center text-muted">面试官马上就到，开场白和第一个问题稍后出现。</p>
        )}

        {state.messages.map((message) => {
          const isCandidate = message.role === "candidate";
          return (
            <article
              key={message.id}
              className={`grid grid-cols-[4.5rem_minmax(0,1fr)] gap-4 border-b border-line-soft py-4 last:border-b-0 ${
                isCandidate ? "bg-wash/40" : ""
              }`}
            >
              <span
                className={`pl-1 pt-0.5 text-right font-serif text-sm ${isCandidate ? "text-accent" : "text-muted"}`}
              >
                {isCandidate ? "你" : "面试官"}
              </span>
              <div className="whitespace-pre-wrap pr-2 leading-relaxed text-ink wrap-anywhere">
                {message.text}
                {message.role === "interviewer" &&
                  !message.done &&
                  message.id === state.activeInterviewerId &&
                  state.activity === "speaking" && (
                    <span aria-hidden="true" className="ml-1 inline-block animate-pulse text-accent">
                      ▍
                    </span>
                  )}
              </div>
            </article>
          );
        })}
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
        className="flex flex-col gap-3"
      >
        <div className="flex items-baseline justify-between">
          <label htmlFor="answer" className="text-sm text-muted">
            你的回答
          </label>
          <span className="text-xs text-faint">{count} / 4000</span>
        </div>
        <textarea
          id="answer"
          rows={4}
          className={`${input} resize-y leading-relaxed`}
          disabled={!canInput}
          value={draft}
          placeholder={placeholder}
          onChange={(event) => {
            if (Array.from(event.target.value).length <= 4000) {
              setDraft(event.target.value);
            }
          }}
          onKeyDown={(event) => {
            if (
              shouldSubmit({
                key: event.key,
                shiftKey: event.shiftKey,
                isComposing: event.nativeEvent.isComposing,
                keyCode: event.nativeEvent.keyCode,
              })
            ) {
              event.preventDefault();
              submit();
            }
          }}
        />
        <div className="flex flex-wrap items-center justify-between gap-3">
          <span className="text-xs text-faint">Enter 发送，Shift + Enter 换行</span>
          <button
            type="submit"
            className={buttonPrimary}
            disabled={!canInput || !draft.trim() || count > 4000}
          >
            发送
          </button>
        </div>
      </form>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4">
        <div className="flex flex-wrap items-center gap-5">
          <button
            type="button"
            className="text-sm text-muted underline-offset-4 hover:text-ink hover:underline cursor-pointer disabled:cursor-not-allowed disabled:text-faint disabled:no-underline"
            disabled={!enabled.repeat || pending}
            onClick={() => command("repeat")}
          >
            再说一遍题目
          </button>
          <button
            type="button"
            className="text-sm text-muted underline-offset-4 hover:text-ink hover:underline cursor-pointer disabled:cursor-not-allowed disabled:text-faint disabled:no-underline"
            disabled={!enabled.skip || pending}
            onClick={() => command("skip")}
          >
            跳过这题
          </button>
          <button
            type="button"
            className="text-sm text-muted underline-offset-4 hover:text-ink hover:underline cursor-pointer disabled:cursor-not-allowed disabled:text-faint disabled:no-underline"
            disabled={pending || !(state.activity === "paused" ? enabled.resume : enabled.pause)}
            onClick={() => command(state.activity === "paused" ? "resume" : "pause")}
          >
            {state.activity === "paused" ? "继续面试" : "暂停"}
          </button>
        </div>
        <button
          type="button"
          className={buttonDanger}
          disabled={!enabled.end || pending}
          onClick={() => confirmation.current?.showModal()}
        >
          结束面试
        </button>
      </div>

      <dialog
        ref={confirmation}
        aria-labelledby="end-title"
        className="fixed inset-0 m-auto max-w-md rounded-lg border border-line bg-sheet p-6 text-ink backdrop:bg-ink/30"
      >
        <h2 id="end-title" className="text-xl text-ink">
          现在结束这场面试？
        </h2>
        <p className="mt-2 text-sm text-muted">
          后面的题目不会再问，报告只根据已经答过的部分生成。结束后不能再接着这一场继续。
        </p>
        <div className="mt-6 flex items-center justify-end gap-3">
          <button
            type="button"
            className={buttonSecondary}
            onClick={() => confirmation.current?.close()}
          >
            再想想
          </button>
          <button
            type="button"
            className={buttonDanger}
            disabled={!enabled.end || pending}
            onClick={() => {
              if (command("end")) confirmation.current?.close();
            }}
          >
            结束并生成报告
          </button>
        </div>
      </dialog>
    </section>
  );
}
