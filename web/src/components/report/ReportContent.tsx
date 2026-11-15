"use client";

import { useEffect, useRef } from "react";
import type { components } from "@/lib/api/schema";
import { formatScore, formatOffset, groupQuestions, locateQuote, turnIndex } from "@/lib/report";
import { turnRoleLabels } from "@/lib/labels";
import { IconChevronDown, IconChevronUp } from "@/components/icons";

type S = components["schemas"];
export type Selection = { turnId: string; quote: string };

type QuoteProps = {
  turns: ReturnType<typeof turnIndex> | null;
  onSelect: (turnId: string, quote: string) => void;
};

export function QuoteButton({
  evidence,
  turns,
  onSelect,
}: QuoteProps & { evidence: S["EvidenceQuote"] }) {
  const missing = turns !== null && !turns.has(evidence.turn_id);
  return (
    <button
      type="button"
      className="group flex w-full min-w-0 cursor-pointer items-baseline gap-2 border-l-2 border-accent-line py-0.5 pl-3 text-left text-sm text-ink-soft transition-colors hover:border-accent disabled:cursor-not-allowed disabled:opacity-50"
      disabled={missing}
      title={missing ? "对话记录里找不到这句话" : turns ? "在对话记录里找到这句话" : "对话记录加载后可以定位"}
      onClick={() => onSelect(evidence.turn_id, evidence.quote)}
    >
      <span className="min-w-0 flex-1 font-serif group-hover:text-ink">“{evidence.quote}”</span>
      <span className="shrink-0 text-xs text-faint">{formatOffset(evidence.start_ms)}</span>
    </button>
  );
}

function Quotes({ evidence, ...props }: QuoteProps & { evidence: S["EvidenceQuote"][] }) {
  if (!evidence || evidence.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      <span className="text-xs text-muted">你的原话</span>
      {evidence.map((item) => (
        <QuoteButton key={`${item.turn_id}-${item.quote}`} {...props} evidence={item} />
      ))}
    </div>
  );
}

function Score({ value, size = "md" }: { value: number | null; size?: "md" | "lg" }) {
  return (
    <span className={`font-serif text-ink ${size === "lg" ? "text-6xl leading-none" : "text-2xl"}`}>
      {formatScore(value)}
    </span>
  );
}

export function ReportSummary({ report }: { report: S["ReportBody"] }) {
  return (
    <section className="flex flex-col gap-8">
      <div className="grid grid-cols-1 gap-6 border-y border-line py-8 sm:grid-cols-[auto_minmax(0,1fr)] sm:gap-10">
        <div className="flex flex-col gap-2">
          <span className="text-sm text-muted">综合得分</span>
          <Score value={report.overall_score} size="lg" />
        </div>
        <p className="whitespace-pre-wrap leading-relaxed text-ink-soft">{report.overall}</p>
      </div>

      {report.top_improvements.length > 0 && (
        <div className="flex flex-col gap-3">
          <h2 className="text-2xl text-ink">最该改进的几点</h2>
          <ol className="flex flex-col">
            {report.top_improvements.map((text, i) => (
              <li key={i} className="grid grid-cols-[2rem_minmax(0,1fr)] border-b border-line py-3">
                <span className="font-serif text-lg text-faint">{i + 1}</span>
                <p className="leading-relaxed text-ink-soft">{text}</p>
              </li>
            ))}
          </ol>
        </div>
      )}

      {report.degraded.length > 0 && (
        <div role="status" className="border-l-2 border-warn pl-3 text-sm">
          <p className="text-warn-ink">下面这些题的评估没有完整完成，分数只作参考：</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-muted">
            {report.degraded.map((text, i) => (
              <li key={i}>{text}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

export function CompetencyList({ items, ...props }: QuoteProps & { items: S["CompetencyReview"][] }) {
  return (
    <section className="flex flex-col gap-4">
      <h2 className="text-2xl text-ink">分项能力</h2>
      {[false, true].map((unassessed) => {
        const filtered = items.filter((item) => (item.score === null) === unassessed);
        if (filtered.length === 0) return null;
        return (
          <div key={String(unassessed)} className="flex flex-col">
            {unassessed && <h3 className="mt-6 mb-2 text-sm text-muted">这场没有充分考察到</h3>}
            {filtered.map((item) => (
              <article key={item.name} className="flex flex-col gap-3 border-t border-line py-5">
                <div className="flex items-baseline justify-between gap-4">
                  <span className="text-lg text-ink">{item.name}</span>
                  <span className="flex shrink-0 items-baseline gap-3">
                    <span className="text-xs text-faint">权重 {Math.round(item.weight * 100)}%</span>
                    <Score value={item.score} />
                  </span>
                </div>
                <p className="leading-relaxed text-ink-soft">{item.conclusion}</p>
                <Quotes {...props} evidence={item.evidence} />
              </article>
            ))}
          </div>
        );
      })}
    </section>
  );
}

function FeedbackList({ title, items, tone }: { title: string; items: string[]; tone: "good" | "gap" }) {
  return (
    <div className="flex flex-col gap-1.5">
      <h4 className={`text-sm ${tone === "good" ? "text-accent" : "text-warn"}`}>{title}</h4>
      {items.length ? (
        <ul className="list-disc space-y-1 pl-5 text-sm text-ink-soft">
          {items.map((text, i) => (
            <li key={i}>{text}</li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-faint">没有单独记录</p>
      )}
    </div>
  );
}

export function QuestionFeedback({ questions, ...props }: QuoteProps & { questions: S["QuestionReview"][] }) {
  const groups = groupQuestions(questions);
  return (
    <section className="flex flex-col gap-4">
      <h2 className="text-2xl text-ink">
        逐题回看<span className="ml-2 text-base text-muted">{groups.asked.length} 道</span>
      </h2>
      <div className="flex flex-col">
        {groups.asked.map((item, index) => (
          <article key={item.question_id} className="flex flex-col gap-5 border-t border-line py-6">
            <div className="flex items-start justify-between gap-4">
              <div className="flex min-w-0 items-start gap-3">
                <span className="w-6 shrink-0 font-serif text-lg leading-7 text-faint">{index + 1}</span>
                <div className="min-w-0">
                  <h3 className="font-serif text-lg leading-7 text-ink">{item.question}</h3>
                  <p className="mt-1 text-sm text-muted">{item.competency}</p>
                </div>
              </div>
              <Score value={item.score ?? null} />
            </div>
            <div className="flex flex-col gap-5 pl-9">
              <p className="leading-relaxed text-ink-soft">
                <span className="mr-2 text-sm text-muted">你答了</span>
                {item.summary}
              </p>
              <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
                <FeedbackList title="答得好的地方" items={item.strengths ?? []} tone="good" />
                <FeedbackList title="漏掉或不足" items={item.missing_points ?? []} tone="gap" />
              </div>
              {item.improved_answer && (
                <div className="flex flex-col gap-2 bg-wash/60 px-4 py-3">
                  <h4 className="text-sm text-accent-ink">可以这样答</h4>
                  <p className="whitespace-pre-wrap leading-relaxed text-ink-soft">{item.improved_answer}</p>
                </div>
              )}
              <Quotes {...props} evidence={item.evidence ?? []} />
            </div>
          </article>
        ))}
      </div>
      {groups.unasked.length > 0 && (
        <div className="border-t border-line pt-5">
          <h3 className="mb-2 text-sm text-muted">时间不够，没来得及问的题</h3>
          <ul className="list-disc space-y-1 pl-5 text-sm text-ink-soft">
            {groups.unasked.map((item) => (
              <li key={item.question_id}>{item.question}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

export function TranscriptPanel({
  turns,
  expanded,
  onToggle,
  selected,
}: {
  turns: S["TranscriptTurnOut"][];
  expanded: boolean;
  onToggle: () => void;
  selected: Selection | null;
}) {
  const target = useRef<HTMLLIElement>(null);

  useEffect(() => {
    if (expanded && selected && target.current) {
      target.current.scrollIntoView({ block: "center", behavior: "smooth" });
      target.current.focus({ preventScroll: true });
    }
  }, [expanded, selected]);

  return (
    <section className="flex flex-col gap-4 border-t border-line pt-8">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="text-2xl text-ink">
          对话记录<span className="ml-2 text-base text-muted">{turns.length} 轮</span>
        </h2>
        <button
          type="button"
          className="inline-flex cursor-pointer items-center gap-1 text-sm text-muted underline-offset-4 hover:text-ink hover:underline print:hidden"
          aria-expanded={expanded}
          aria-controls="report-transcript"
          onClick={onToggle}
        >
          {expanded ? "收起" : "展开"}
          {expanded ? <IconChevronUp className="size-3.5" /> : <IconChevronDown className="size-3.5" />}
        </button>
      </div>

      <div id="report-transcript" hidden={!expanded}>
        {turns.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted">这场没有对话记录。</p>
        ) : (
          <ol className="flex flex-col">
            {turns.map((turn) => {
              const active = selected?.turnId === turn.id;
              const match = active ? locateQuote(turn.text, selected.quote) : null;
              const isInterviewer = turn.role === "interviewer";
              return (
                <li
                  key={turn.id}
                  ref={active ? target : undefined}
                  tabIndex={-1}
                  className={`grid grid-cols-[4.5rem_minmax(0,1fr)] gap-4 border-b border-line-soft py-3 outline-none ${
                    active ? "bg-accent-wash/60" : isInterviewer ? "" : "bg-wash/40"
                  }`}
                >
                  <span className="pl-1 text-right">
                    <span className={`block font-serif text-sm ${isInterviewer ? "text-muted" : "text-accent"}`}>
                      {turnRoleLabels[turn.role] || turn.role}
                    </span>
                    <span className="block text-[11px] text-faint">{formatOffset(turn.start_ms)}</span>
                  </span>
                  <p className="whitespace-pre-wrap pr-2 leading-relaxed text-ink wrap-anywhere">
                    {!active ? (
                      turn.text
                    ) : !match ? (
                      <mark className="bg-warn-wash px-0.5 text-ink">{turn.text}</mark>
                    ) : (
                      <>
                        {turn.text.slice(0, match.start)}
                        <mark className="bg-warn-wash px-0.5 text-ink">
                          {turn.text.slice(match.start, match.end)}
                        </mark>
                        {turn.text.slice(match.end)}
                      </>
                    )}
                  </p>
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </section>
  );
}
