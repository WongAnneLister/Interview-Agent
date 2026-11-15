"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { ErrorNotice } from "@/components/ErrorNotice";
import { LoadingNotice } from "@/components/LoadingNotice";
import { listHistoryQuestions } from "@/lib/api/client";
import { groupHistory, type GroupBy, type HistoryQuestion } from "@/lib/history";
import { interviewStatusLabels, stageLabels } from "@/lib/labels";
import { input } from "@/lib/styles";
import { useRequest } from "@/lib/useRequest";

const groupOptions: Array<{ value: GroupBy; label: string }> = [
  { value: "job", label: "按岗位" },
  { value: "stage", label: "按题型" },
  { value: "competency", label: "按能力" },
];

function meta(item: HistoryQuestion, by: GroupBy): string {
  const q = item.question;
  return [
    by !== "job" && item.job_title,
    by !== "stage" && stageLabels[q.stage],
    by !== "competency" && q.competency,
    q.project,
    item.created_at.slice(0, 10),
  ]
    .filter(Boolean)
    .join(" · ");
}

function QuestionRow({ item, by }: { item: HistoryQuestion; by: GroupBy }) {
  const [open, setOpen] = useState(false);
  const q = item.question;
  return (
    <li className="border-t border-line">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="group flex w-full cursor-pointer items-start justify-between gap-4 py-4 text-left"
      >
        <span className="min-w-0">
          <span className="block leading-relaxed text-ink group-hover:text-accent-ink">
            {q.text}
          </span>
          <span className="mt-1 block text-sm text-muted">{meta(item, by)}</span>
        </span>
        <span className="shrink-0 pt-0.5 text-sm text-faint group-hover:text-ink">
          {open ? "收起" : "展开"}
        </span>
      </button>
      {open && (
        <div className="grid grid-cols-1 gap-5 pb-5 sm:grid-cols-2">
          <div>
            <h4 className="mb-1.5 text-sm text-muted">考察要点</h4>
            <ul className="list-disc space-y-1 pl-5 text-sm text-ink-soft">
              {q.key_points.map((text, i) => (
                <li key={i}>{text}</li>
              ))}
            </ul>
          </div>
          <div>
            <h4 className="mb-1.5 text-sm text-muted">追问方向</h4>
            <ul className="list-disc space-y-1 pl-5 text-sm text-ink-soft">
              {q.follow_ups.map((text, i) => (
                <li key={i}>{text}</li>
              ))}
            </ul>
          </div>
          <p className="text-sm text-muted sm:col-span-2">
            这场面试{interviewStatusLabels[item.status]}。
            <Link
              className="ml-1 text-accent underline-offset-4 hover:underline"
              href={`/interviews/${encodeURIComponent(item.interview_id)}/plan`}
            >
              看这场的计划
            </Link>
          </p>
        </div>
      )}
    </li>
  );
}

export default function HistoryQuestionsPage() {
  const { run, cancel, busy, error } = useRequest();
  const [items, setItems] = useState<HistoryQuestion[] | null>(null);
  const [by, setBy] = useState<GroupBy>("job");
  const [query, setQuery] = useState("");

  function load() {
    void run((signal) => listHistoryQuestions(signal), setItems);
  }

  useEffect(() => {
    void run((signal) => listHistoryQuestions(signal), setItems);
    return cancel;
  }, [run, cancel]);

  const groups = useMemo(() => groupHistory(items ?? [], by, query), [items, by, query]);
  const shown = groups.reduce((sum, group) => sum + group.items.length, 0);
  const interviews = new Set((items ?? []).map((item) => item.interview_id)).size;

  return (
    <div className="flex min-w-0 flex-col gap-10">
      <header className="flex flex-col gap-3">
        <h1 className="text-[32px] leading-tight text-ink sm:text-[38px]">历史面试题</h1>
        <p className="text-muted">
          每次贴职位描述生成的题目都收在这里
          {items && items.length > 0 && `，共 ${interviews} 场、${items.length} 道`}。
        </p>
      </header>

      {busy && !items && <LoadingNotice message="正在读取题目" />}
      <ErrorNotice error={error} onRetry={load} />

      {items && items.length === 0 && (
        <p className="border-y border-line py-10 text-center text-muted">
          还没有生成过题目。
          <Link className="ml-1 text-accent underline-offset-4 hover:underline" href="/new">
            准备一场面试
          </Link>
        </p>
      )}

      {items && items.length > 0 && (
        <>
          <div className="flex flex-wrap items-end justify-between gap-4 border-b border-line">
            <div className="flex gap-6" role="tablist" aria-label="分组方式">
              {groupOptions.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="tab"
                  aria-selected={by === option.value}
                  onClick={() => setBy(option.value)}
                  className="-mb-px cursor-pointer border-b-2 border-transparent pb-3 text-sm text-muted transition-colors hover:text-ink aria-selected:border-accent aria-selected:text-ink"
                >
                  {option.label}
                </button>
              ))}
            </div>
            <label className="mb-2 w-full sm:w-64">
              <span className="sr-only">搜索题目</span>
              <input
                className={input}
                type="search"
                value={query}
                placeholder="搜题目、要点或岗位"
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
          </div>

          {groups.length === 0 ? (
            <p className="py-6 text-center text-muted">没有找到包含“{query}”的题目。</p>
          ) : (
            <div className="flex flex-col gap-12" role="tabpanel">
              {query && <p className="-mt-6 text-sm text-muted">找到 {shown} 道</p>}
              {groups.map((group) => (
                <section key={group.key} className="flex flex-col">
                  <h2 className="mb-2 text-2xl text-ink">
                    {group.title}
                    <span className="ml-2 font-sans text-base text-muted">
                      {group.items.length} 道
                    </span>
                  </h2>
                  <ul>
                    {group.items.map((item) => (
                      <QuestionRow
                        key={`${item.interview_id}-${item.question.id}`}
                        item={item}
                        by={by}
                      />
                    ))}
                  </ul>
                </section>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
