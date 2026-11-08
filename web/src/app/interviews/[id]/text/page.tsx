"use client";
import { useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { ErrorNotice } from "@/components/ErrorNotice";
import { phaseLabels } from "@/lib/labels";
import { controls } from "@/lib/interview/session";
import { shouldSubmit } from "@/lib/interview/composer";
import { useInterviewSession } from "@/lib/interview/useInterviewSession";
import {
  buttonPrimary,
  buttonSecondary,
  buttonDanger,
  input,
  panel,
} from "@/lib/styles";
const activityLabels = {
  listening: "聆听中",
  thinking: "面试官思考中",
  speaking: "面试官说话中",
  paused: "已暂停",
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
  const connectionLabel = state.connection === "connecting"
    ? "连接中"
    : state.connection === "reconnecting"
      ? `重连中（第 ${attempt} 次）`
      : state.connection === "failed"
        ? "已断开"
        : state.connection === "ended"
          ? "已结束"
          : "已连接";
  const placeholder = state.connection !== "open"
    ? "连接恢复后即可作答…"
    : pending
      ? "正在发送，请稍候…"
      : state.activity === "paused"
        ? "已暂停，点继续后作答"
        : state.activity === "speaking"
          ? "面试官正在说话…"
          : state.activity === "thinking"
            ? "面试官正在思考…"
            : "输入你的回答，Enter 发送，Shift+Enter 换行";
  useEffect(() => {
    if (nearBottom.current && transcript.current) {
      transcript.current.scrollTop = transcript.current.scrollHeight;
    }
  }, [state.messages]);
  function submit() {
    if (sendText(draft)) setDraft("");
  }
  return (
    <section className="flex min-w-0 flex-col gap-5 pt-9 sm:pt-0">
      <header className="fixed inset-x-0 top-0 z-20 border-b border-border bg-[#f6f8f5] px-4 py-3 shadow-sm">
        <div className="mx-auto flex max-w-[944px] flex-wrap items-center justify-between gap-2 text-sm">
          <h1 className="font-bold">
            文字面试 · {state.phase ? phaseLabels[state.phase] : "准备中"}
          </h1>
          <p>
            本阶段 <span className="font-mono">{time(state.stageRemainingS)}</span> · 总剩余 <span className="font-mono">{time(state.totalRemainingS)}</span>
          </p>
          <p role="status">
            {state.activity ? activityLabels[state.activity] : "等待面试官"} · {connectionLabel}
          </p>
        </div>
      </header>
      <p className="text-sm text-muted">离开页面后面试会保留，回来可以继续。</p>
      {state.connection === "ended" && (
        <p role="status">面试结束，正在前往报告…</p>
      )}
      {state.error && (
        <div className="flex flex-col items-start gap-2">
          <ErrorNotice error={state.error} />
          <button className={buttonSecondary} onClick={clearError}>
            关闭错误提示
          </button>
        </div>
      )}
      {state.connection === "failed" && (
        <div className="flex flex-wrap items-center gap-3">
          {!state.error && <p>连接已断开，请重新连接。</p>}
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
        className={`${panel} flex h-[42dvh] min-h-56 flex-col gap-4 overflow-y-auto overscroll-contain`}
      >
        {state.resumed && (
          <p className="text-sm text-muted">
            已恢复面试，之前的对话不在此显示；可点‘再说一遍题目’重听当前问题
          </p>
        )}
        {!state.messages.length && !state.resumed && (
          <p className="text-sm text-muted">正在等待面试官开场…</p>
        )}
        {state.messages.map((message) => (
          <article
            key={message.id}
            className={`max-w-[90%] rounded-xl px-4 py-3 ${message.role === "candidate" ? "self-end bg-accent text-white" : "self-start bg-[#eef3ef]"}`}
          >
            <p className="mb-1 text-xs opacity-70">
              {message.role === "candidate" ? "你" : "面试官"}
            </p>
            <p className="whitespace-pre-wrap wrap-anywhere">
              {message.text}
              {message.role === "interviewer" && !message.done &&
                message.id === state.activeInterviewerId &&
                state.activity === "speaking" && (
                  <span aria-hidden="true" className="ml-1 animate-pulse">▍</span>
                )}
            </p>
          </article>
        ))}
      </div>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
        className="flex flex-col gap-3"
      >
        <label htmlFor="answer" className="font-semibold">你的回答</label>
        <textarea
          id="answer"
          rows={4}
          className={input}
          disabled={!canInput}
          value={draft}
          placeholder={placeholder}
          onChange={(event) => {
            if (Array.from(event.target.value).length <= 4000) {
              setDraft(event.target.value);
            }
          }}
          onKeyDown={(event) => {
            if (shouldSubmit({
              key: event.key,
              shiftKey: event.shiftKey,
              isComposing: event.nativeEvent.isComposing,
              keyCode: event.nativeEvent.keyCode,
            })) {
              event.preventDefault();
              submit();
            }
          }}
        />
        <div className="flex items-center justify-between gap-3">
          <p className="text-sm text-muted">
            {count} / 4000 字 · Enter 发送，Shift+Enter 换行
          </p>
          <button
            type="submit"
            className={buttonPrimary}
            disabled={!canInput || !draft.trim() || count > 4000}
          >
            发送回答
          </button>
        </div>
      </form>
      <div className="flex flex-wrap gap-2">
        <button
          className={buttonSecondary}
          disabled={!enabled.repeat || pending}
          onClick={() => command("repeat")}
        >
          再说一遍题目
        </button>
        <button
          className={buttonSecondary}
          disabled={!enabled.skip || pending}
          onClick={() => command("skip")}
        >
          跳过本题
        </button>
        <button
          className={buttonSecondary}
          disabled={pending || !(state.activity === "paused" ? enabled.resume : enabled.pause)}
          onClick={() => command(state.activity === "paused" ? "resume" : "pause")}
        >
          {state.activity === "paused" ? "继续" : "暂停"}
        </button>
        <button
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
        className="fixed inset-0 m-auto max-w-[90vw] rounded-2xl border border-border p-7 shadow-xl backdrop:bg-black/30"
      >
        <h2 id="end-title" className="text-xl font-bold">结束面试？</h2>
        <p className="my-4">结束后将生成报告，不能再继续这场面试</p>
        <div className="flex justify-end gap-3">
          <button
            className={buttonSecondary}
            onClick={() => confirmation.current?.close()}
          >
            继续面试
          </button>
          <button
            className={buttonDanger}
            disabled={!enabled.end || pending}
            onClick={() => {
              if (command("end")) confirmation.current?.close();
            }}
          >
            确认结束
          </button>
        </div>
      </dialog>
    </section>
  );
}
