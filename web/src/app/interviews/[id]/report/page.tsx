import Link from "next/link";
import { buttonPrimary, panel } from "@/lib/styles";
export const metadata = { title: "面试报告 · 面试练习室" };
export default function ReportPage() {
  return (
    <section className={`${panel} flex flex-col items-start gap-5`}>
      <h1 className="text-2xl font-bold">面试已结束</h1>
      <p>面试已结束，报告正在生成；报告页将在下一个版本提供</p>
      <Link className={buttonPrimary} href="/new">
        返回，准备新的面试
      </Link>
    </section>
  );
}
