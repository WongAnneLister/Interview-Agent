"use client";

import { useEffect, useRef } from "react";
import type { components } from "@/lib/api/schema";
import { formatScore, formatOffset, groupQuestions, locateQuote, turnIndex } from "@/lib/report";
import { turnRoleLabels } from "@/lib/labels";
import { buttonSecondary, panel } from "@/lib/styles";

type S = components["schemas"];
export type Selection = { turnId: string; quote: string };
type QuoteProps = {
  // null 表示转写还没读到；此时允许点击，读到后再滚动定位。
  turns: ReturnType<typeof turnIndex> | null;
  onSelect: (turnId: string, quote: string) => void;
};

export function QuoteButton({ evidence, turns, onSelect }: QuoteProps & { evidence: S["EvidenceQuote"] }) {
  const missing = turns !== null && !turns.has(evidence.turn_id);
  return <button
    type="button" className={`${buttonSecondary} whitespace-normal text-left wrap-anywhere`}
    disabled={missing}
    title={missing ? "转写中找不到这句话" : turns ? "在转写中查看原话" : "转写加载后定位到原话"}
    onClick={() => onSelect(evidence.turn_id, evidence.quote)}
  >
    “{evidence.quote}” · {formatOffset(evidence.start_ms)}
  </button>;
}

function Quotes({ evidence, ...props }: QuoteProps & { evidence: S["EvidenceQuote"][] }) {
  return <ul className="flex flex-col items-start gap-2">
    {evidence.map(item => <li key={`${item.turn_id}-${item.quote}`}>
      <QuoteButton {...props} evidence={item} />
    </li>)}
  </ul>;
}

export function ReportSummary({ report }: { report: S["ReportBody"] }) {
  return <section className={`${panel} flex flex-col gap-4`}>
    <h2 className="text-xl font-semibold">总体总结 · {formatScore(report.overall_score)}</h2>
    <p className="whitespace-pre-wrap">{report.overall}</p>
    <h3 className="font-semibold">改进建议</h3>
    <ol className="list-decimal space-y-2 pl-6">
      {report.top_improvements.map((text, i) => <li key={i}>{text}</li>)}
    </ol>
    {report.degraded.length > 0 && <div role="status">
      <p>部分题目评分失败，以下结果不完整</p>
      <ul className="list-disc pl-6">
        {report.degraded.map((text, i) => <li key={i}>{text}</li>)}
      </ul>
    </div>}
  </section>;
}

export function CompetencyList({ items, ...props }: QuoteProps & { items: S["CompetencyReview"][] }) {
  return <section className={`${panel} flex flex-col gap-5`}>
    <h2 className="text-xl font-semibold">能力项得分</h2>
    {[false, true].map(unassessed => <div key={String(unassessed)} className="flex flex-col gap-4">
      {unassessed && items.some(item => item.score === null) &&
        <h3 className="font-semibold">本场未考察到</h3>}
      {items.filter(item => (item.score === null) === unassessed).map(item => <article
        key={item.name} className="flex flex-col gap-2 border-t border-border pt-4"
      >
        <h3 className="font-semibold">{item.name} · {formatScore(item.score)}</h3>
        <p className="text-sm text-muted">权重 {Math.round(item.weight * 100)}%</p>
        <p>{item.conclusion}</p>
        <Quotes {...props} evidence={item.evidence} />
      </article>)}
    </div>)}
  </section>;
}

function FeedbackList({ title, items }: { title: string; items: string[] }) {
  return <div>
    <h4 className="font-semibold">{title}</h4>
    {items.length ? <ul className="list-disc pl-6">
      {items.map((text, i) => <li key={i}>{text}</li>)}
    </ul> : <p className="text-muted">暂无</p>}
  </div>;
}

export function QuestionFeedback({ questions, ...props }: QuoteProps & { questions: S["QuestionReview"][] }) {
  const groups = groupQuestions(questions);
  return <section className="flex flex-col gap-4">
    <h2 className="text-xl font-semibold">逐题反馈</h2>
    {groups.asked.map(item => <article key={item.question_id} className={`${panel} flex flex-col gap-3`}>
      <h3 className="font-semibold">{item.question}</h3>
      <p>{item.competency} · {formatScore(item.score ?? null)}</p>
      <div>
        <h4 className="font-semibold">摘要</h4>
        <p>{item.summary}</p>
      </div>
      <FeedbackList title="优点" items={item.strengths ?? []} />
      <FeedbackList title="缺失要点" items={item.missing_points ?? []} />
      <div>
        <h4 className="font-semibold">参考说法</h4>
        <p className="whitespace-pre-wrap">{item.improved_answer}</p>
      </div>
      <Quotes {...props} evidence={item.evidence ?? []} />
    </article>)}
    {groups.unasked.length > 0 && <section className={panel}>
      <h3 className="mb-3 font-semibold">本场未问到</h3>
      <ul className="list-disc pl-6">
        {groups.unasked.map(item => <li key={item.question_id}>{item.question}</li>)}
      </ul>
    </section>}
  </section>;
}

export function TranscriptPanel({ turns, expanded, onToggle, selected }: {
  turns: S["TranscriptTurnOut"][];
  expanded: boolean;
  onToggle: () => void;
  selected: Selection | null;
}) {
  const target = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (expanded && selected && target.current) {
      target.current.scrollIntoView({ block: "center" });
      target.current.focus({ preventScroll: true });
    }
  }, [expanded, selected]);
  return <section className={panel}>
    <h2>
      <button type="button" className={buttonSecondary} aria-expanded={expanded}
        aria-controls="report-transcript" onClick={onToggle}>
        {expanded ? "收起完整转写" : "展开完整转写"}
      </button>
    </h2>
    <div id="report-transcript" hidden={!expanded} className="mt-4">
      {turns.length === 0 && <p>本场没有转写记录。</p>}
      <ol className="flex flex-col gap-4">{turns.map(turn => {
        const active = selected?.turnId === turn.id;
        const match = active ? locateQuote(turn.text, selected.quote) : null;
        return <li key={turn.id} ref={active ? target : undefined} tabIndex={-1}
          className={`rounded-lg border-l-4 p-4 ${active ? "border-accent bg-[#e9f3ed]" : "border-border"}`}>
          <p className="mb-2 text-sm text-muted">
            {turnRoleLabels[turn.role]} · {formatOffset(turn.start_ms)}
          </p>
          <p className="whitespace-pre-wrap wrap-anywhere">
            {!active ? turn.text : !match ? <mark>{turn.text}</mark> : <>
              {turn.text.slice(0, match.start)}
              <mark>{turn.text.slice(match.start, match.end)}</mark>
              {turn.text.slice(match.end)}
            </>}
          </p>
        </li>;
      })}</ol>
    </div>
  </section>;
}
