"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo, useState } from "react";
import { ErrorNotice } from "@/components/ErrorNotice";
import { LoadingNotice } from "@/components/LoadingNotice";
import { ReportSummary, CompetencyList, QuestionFeedback, TranscriptPanel, type Selection } from "@/components/report/ReportContent";
import { turnIndex } from "@/lib/report";
import { useReport } from "@/lib/report/useReport";
import { buttonPrimary, buttonSecondary } from "@/lib/styles";

export default function ReportPage() {
  const { id } = useParams<{ id: string }>();
  return <ReportSession key={id} id={id} />;
}

function ReportSession({ id }: { id: string }) {
  const report = useReport(id);
  const [expanded, setExpanded] = useState(false);
  const [selected, setSelected] = useState<Selection | null>(null);
  const body = report.status === "ready" ? report.data?.report : null;

  const turns = useMemo(
    () => (report.transcript ? turnIndex(report.transcript.turns) : null),
    [report.transcript],
  );

  const quotes = {
    turns,
    onSelect(turnId: string, quote: string) {
      setExpanded(true);
      setSelected({ turnId, quote });
    },
  };

  return (
    <div className="flex min-w-0 flex-col gap-12">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-2">
          <h1 className="text-[32px] leading-tight text-ink sm:text-[38px]">面试报告</h1>
          <p className="text-xs text-faint">编号 {id}</p>
        </div>
        {body && (
          <button
            type="button"
            className="cursor-pointer text-sm text-muted underline-offset-4 hover:text-ink hover:underline print:hidden"
            onClick={() => window.print()}
          >
            打印或存成 PDF
          </button>
        )}
      </header>

      {report.status === "loading" && (
        <LoadingNotice key="load" message="正在读取报告" />
      )}

      {report.status === "pending" && (
        <LoadingNotice
          key="pending"
          message="报告正在生成，通常 30–60 秒"
        />
      )}

      <ErrorNotice error={report.error} onRetry={report.retry} />

      {report.status === "failed" && (
        <section className="flex flex-col items-start gap-3 border-l-2 border-danger pl-4">
          <div>
            <h2 className="text-xl text-danger-ink">报告没有生成出来</h2>
            {report.data?.error && (
              <p className="mt-1 text-sm text-danger">
                原因代码 <code className="font-mono">{report.data.error}</code>
              </p>
            )}
          </div>
          <button
            className={buttonPrimary}
            disabled={report.regenerating}
            onClick={report.regenerate}
          >
            {report.regenerating ? "正在重新生成…" : "重新生成"}
          </button>
        </section>
      )}

      {report.status === "not_ended" && (
        <section className="flex flex-col items-start gap-3 border-l-2 border-line pl-4">
          <div>
            <h2 className="text-xl text-ink">面试还没结束</h2>
            <p className="mt-1 text-sm text-muted">
              答完全部题目，或者在面试里点“结束面试”，才会生成报告。
            </p>
          </div>
          <Link
            className={buttonPrimary}
            href={`/interviews/${encodeURIComponent(id)}/text`}
          >
            回到面试
          </Link>
        </section>
      )}

      {body && (
        <>
          <ReportSummary report={body} />
          <CompetencyList items={body.competencies} {...quotes} />
          <QuestionFeedback questions={body.questions} {...quotes} />

          <ErrorNotice
            error={report.transcriptError}
            onRetry={report.retryTranscript}
          />

          {!report.transcript && !report.transcriptError && (
            <LoadingNotice message="正在读取对话记录" />
          )}

          {report.transcript && (
            <TranscriptPanel
              turns={report.transcript.turns}
              expanded={expanded}
              onToggle={() => setExpanded((value) => !value)}
              selected={selected}
            />
          )}

          {report.data?.disclaimer && (
            <p className="border-t border-line pt-4 text-xs text-faint">
              {report.data.disclaimer}
            </p>
          )}
        </>
      )}

      <div className="print:hidden">
        <Link className={buttonSecondary} href="/new">
          准备下一场
        </Link>
      </div>
    </div>
  );
}
