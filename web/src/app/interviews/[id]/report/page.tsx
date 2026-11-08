"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo, useState } from "react";
import { ErrorNotice } from "@/components/ErrorNotice";
import { LoadingNotice } from "@/components/LoadingNotice";
import {
  ReportSummary, CompetencyList, QuestionFeedback, TranscriptPanel, type Selection,
} from "@/components/report/ReportContent";
import { turnIndex } from "@/lib/report";
import { useReport } from "@/lib/report/useReport";
import { buttonPrimary, buttonSecondary, panel } from "@/lib/styles";

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
  return <div className="flex min-w-0 flex-col gap-6">
    <h1 className="text-3xl font-bold">面试报告</h1>
    {report.status === "loading" && <LoadingNotice key="load" message="正在读取报告…" />}
    {report.status === "pending" &&
      <LoadingNotice key="pending" message="报告生成中，通常一分钟内完成" />}
    <ErrorNotice error={report.error} onRetry={report.retry} />
    {report.status === "failed" && <section className={`${panel} flex flex-col items-start gap-4`}>
      <h2 className="text-xl font-semibold">报告生成失败</h2>
      {report.data?.error && <p>原因码：{report.data.error}</p>}
      <button className={buttonPrimary} disabled={report.regenerating} onClick={report.regenerate}>
        {report.regenerating ? "正在重新生成…" : "重新生成"}
      </button>
    </section>}
    {report.status === "not_ended" && <section className={panel}>
      <p className="mb-4">面试还没有结束，结束后才会生成报告</p>
      <Link className={buttonSecondary} href={`/interviews/${encodeURIComponent(id)}/text`}>
        返回文字面试
      </Link>
    </section>}
    {body && <>
      <ReportSummary report={body} />
      <CompetencyList items={body.competencies} {...quotes} />
      <QuestionFeedback questions={body.questions} {...quotes} />
      <ErrorNotice error={report.transcriptError} onRetry={report.retryTranscript} />
      {!report.transcript && !report.transcriptError && <LoadingNotice message="正在读取转写…" />}
      {report.transcript && <TranscriptPanel
        turns={report.transcript.turns} expanded={expanded}
        onToggle={() => setExpanded(value => !value)} selected={selected}
      />}
      <p className="text-sm text-muted">{report.data?.disclaimer}</p>
    </>}
    <div><Link className={buttonSecondary} href="/new">返回，准备新的面试</Link></div>
  </div>;
}
