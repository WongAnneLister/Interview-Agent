"use client";

import type { components } from "@/lib/api/schema";
import { stageLabels } from "@/lib/labels";
import { formatMinutes, groupQuestions } from "@/lib/plan";
import { QuestionCard } from "./QuestionCard";

type Plan = components["schemas"]["PlanOut"];
type Question = components["schemas"]["Question"];

export function PlanView({
  plan,
  questions,
  expanded,
  readOnly,
  busy,
  onExpanded,
  onQuestions,
}: {
  plan: Plan;
  questions: Question[];
  expanded: Set<string>;
  readOnly: boolean;
  busy: boolean;
  onExpanded: (ids: Set<string>) => void;
  onQuestions: (questions: Question[]) => void;
}) {
  const totalSeconds = plan.stages.reduce((sum, slot) => sum + slot.seconds, 0);

  return (
    <div className="flex min-w-0 flex-col gap-12">
      <section className="flex min-w-0 flex-col gap-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-2xl text-ink">时间安排</h2>
          <span className="text-sm text-muted">
            全场 {formatMinutes(totalSeconds)} 分钟
          </span>
        </div>
        <div className="flex h-2 w-full gap-0.5 overflow-hidden rounded-sm" aria-hidden="true">
          {plan.stages.map((slot, index) => (
            <div
              key={slot.stage}
              className={index % 2 ? "bg-accent/70" : "bg-accent"}
              style={{ flexGrow: slot.seconds || 1 }}
            />
          ))}
        </div>
        <ol className="grid grid-cols-2 gap-x-8 border-t border-line sm:grid-cols-3">
          {plan.stages.map((slot) => (
            <li
              key={slot.stage}
              className="flex items-baseline justify-between gap-3 border-b border-line py-2.5"
            >
              <span className="truncate text-sm text-ink-soft">
                {stageLabels[slot.stage] || slot.stage}
              </span>
              <span className="shrink-0 font-serif text-ink">
                {formatMinutes(slot.seconds)}
                <span className="ml-0.5 text-xs text-muted">分</span>
              </span>
            </li>
          ))}
        </ol>
      </section>

      <section className="flex min-w-0 flex-col gap-4">
        <h2 className="text-2xl text-ink">考察什么</h2>
        {plan.competencies.length === 0 ? (
          <p className="text-sm text-muted">这份计划没有列出能力项。</p>
        ) : (
          <ul className="border-t border-line">
            {plan.competencies.map((item, i) => {
              const pct = Math.round(item.weight * 100);
              return (
                <li
                  key={i}
                  className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1.5 border-b border-line py-3 sm:grid-cols-[minmax(0,1fr)_10rem_3rem]"
                >
                  <span className={item.covered ? "text-ink" : "text-faint"}>
                    {item.name}
                  </span>
                  {item.covered ? (
                    <>
                      <span className="order-last col-span-2 h-1 overflow-hidden rounded-sm bg-line sm:order-none sm:col-span-1">
                        <span
                          className="block h-full bg-accent"
                          style={{ width: `${pct}%` }}
                        />
                      </span>
                      <span className="text-right font-serif text-ink">{pct}%</span>
                    </>
                  ) : (
                    <span className="text-right text-sm text-faint sm:col-span-2">
                      本场不考察
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section className="flex min-w-0 flex-col gap-4">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <div>
            <h2 className="text-2xl text-ink">
              题目<span className="ml-2 text-base text-muted">{questions.length} 道</span>
            </h2>
            <p className="mt-1 text-sm text-muted">
              题目默认收起，留到面试时再听更接近真实；想先看或修改也可以展开。
            </p>
          </div>
          <div className="flex items-center gap-4 text-sm">
            <button
              type="button"
              className="cursor-pointer text-muted underline-offset-4 hover:text-ink hover:underline"
              onClick={() => onExpanded(new Set(questions.map((q) => q.id)))}
            >
              全部展开
            </button>
            <button
              type="button"
              className="cursor-pointer text-muted underline-offset-4 hover:text-ink hover:underline"
              onClick={() => onExpanded(new Set())}
            >
              全部收起
            </button>
          </div>
        </div>

        {questions.length === 0 && (
          <p className="border-y border-line py-8 text-center text-sm text-muted">
            题目都删掉了。点下方的“放弃修改”可以恢复原来的题目。
          </p>
        )}

        {groupQuestions(questions, plan.stages)
          .filter((group) => group.questions.length)
          .map((group) => (
            <section key={group.stage} className="mt-2 flex min-w-0 flex-col gap-3">
              <h3 className="text-sm text-muted">
                {stageLabels[group.stage] || group.stage}
                <span className="ml-2 text-faint">{group.questions.length} 道</span>
              </h3>
              <div className="flex flex-col gap-3">
                {group.questions.map((question) => (
                  <QuestionCard
                    key={question.id}
                    question={question}
                    number={questions.findIndex((q) => q.id === question.id) + 1}
                    expanded={expanded.has(question.id)}
                    readOnly={readOnly}
                    busy={busy}
                    onToggle={() => {
                      const next = new Set(expanded);
                      if (next.has(question.id)) next.delete(question.id);
                      else next.add(question.id);
                      onExpanded(next);
                    }}
                    onChange={(updated) =>
                      onQuestions(
                        questions.map((q) => (q.id === updated.id ? updated : q)),
                      )
                    }
                    onDelete={() => {
                      const next = new Set(expanded);
                      next.delete(question.id);
                      onExpanded(next);
                      onQuestions(questions.filter((q) => q.id !== question.id));
                    }}
                  />
                ))}
              </div>
            </section>
          ))}
      </section>
    </div>
  );
}
