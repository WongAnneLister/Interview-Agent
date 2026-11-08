"use client";
import { buttonSecondary, panel } from "@/lib/styles";
import Link from "next/link";
import { useParams } from "next/navigation";
export default function TextInterviewPage() {
  const { id } = useParams<{ id: string }>();
  return (
    <section className={`${panel} flex min-w-0 flex-col gap-4`}>
      <span className="text-xs font-bold tracking-[0.12em] text-accent">
        下一步 · 文字面试
      </span>
      <h1 className="text-[clamp(28px,4vw,38px)] leading-[1.35] font-bold tracking-[-0.04em]">
        文字面试页将在下一个版本提供
      </h1>
      <p className="text-muted">面试计划已保存，你可以随时返回查看。</p>
      <div>
        <Link className={buttonSecondary} href={`/interviews/${id}/plan`}>
          返回计划
        </Link>
      </div>
    </section>
  );
}
