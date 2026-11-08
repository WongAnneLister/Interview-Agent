"use client";
import { buttonSecondary, panel } from "@/lib/styles";
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
  return (
    <div className="flex min-w-0 flex-col gap-7">
      <section className={`${panel} flex min-w-0 flex-col gap-4`}>
        <h2 className="text-[21px] font-[650]">阶段时间分配</h2>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          {plan.stages.map((slot) => (
            <div
              key={slot.stage}
              className="flex min-w-0 flex-col gap-[5px] rounded-lg bg-[#f3f6f0] p-4 text-[13px]"
            >
              <span>{stageLabels[slot.stage]}</span>
              <strong className="text-2xl font-semibold">
                {formatMinutes(slot.seconds)}{" "}
                <small className="text-xs font-normal text-muted">分钟</small>
              </strong>
            </div>
          ))}
        </div>
        <p className="text-muted">
          合计{" "}
          {formatMinutes(
            plan.stages.reduce((sum, slot) => sum + slot.seconds, 0),
          )}{" "}
          分钟；各阶段按半分钟取整展示。
        </p>
      </section>
      <section className={`${panel} flex min-w-0 flex-col gap-4`}>
        <h2 className="text-[21px] font-[650]">本场能力项</h2>
        {plan.competencies.length === 0 && (
          <p className="text-muted">暂无能力项</p>
        )}
        <div className="flex flex-wrap gap-3">
          {plan.competencies.map((item, i) => (
            <div
              key={i}
              className={`flex flex-wrap items-center gap-3 rounded-lg border border-border px-4 py-3 text-sm ${item.covered ? "" : "bg-[#f4f4f2] text-muted"}`}
            >
              <strong>{item.name}</strong>
              <span>权重 {Math.round(item.weight * 100)}%</span>
              {!item.covered && (
                <span className="inline-block rounded-md bg-[#edf4ee] px-2.5 py-[3px] text-[13px] text-accent">
                  本场不考察
                </span>
              )}
            </div>
          ))}
        </div>
      </section>
      <section className="flex min-w-0 flex-col gap-4">
        <div className="flex flex-wrap items-start justify-between gap-4 sm:flex-nowrap">
          <div>
            <h2 className="text-[21px] font-[650]">题目预览</h2>
            <p className="text-muted">
              题目默认隐藏，避免提前看到影响练习效果。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <button
              className={buttonSecondary}
              onClick={() => onExpanded(new Set(questions.map((q) => q.id)))}
            >
              全部展开
            </button>
            <button
              className={buttonSecondary}
              onClick={() => onExpanded(new Set())}
            >
              全部收起
            </button>
          </div>
        </div>
        {questions.length === 0 && (
          <p className={`${panel} text-muted`}>
            暂无题目。保存时后端会检查必考能力项是否有题目；可放弃修改恢复。
          </p>
        )}
        {groupQuestions(questions, plan.stages)
          .filter((group) => group.questions.length)
          .map((group) => (
            <section key={group.stage} className="flex min-w-0 flex-col gap-4">
              <h3 className="text-base font-[650] text-accent">
                {stageLabels[group.stage]}
              </h3>
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
            </section>
          ))}
      </section>
    </div>
  );
}
