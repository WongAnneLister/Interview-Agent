import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
export const metadata: Metadata = {
  title: "面试练习室",
  description: "根据简历与目标岗位准备模拟面试，让每一次练习更有针对性。",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <header className="mx-auto flex max-w-[1120px] items-center justify-between gap-4 border-b border-border px-5 py-[18px] sm:px-8 sm:py-[26px]">
          <Link
            href="/new"
            className="flex items-center gap-2.5 text-xl font-[750] text-ink hover:underline"
          >
            面试练习室
            <span
              className="size-[9px] shrink-0 rounded-full bg-[#b78037]"
              aria-hidden="true"
            />
          </Link>
          <span className="hidden text-muted sm:inline">
            为下一场面试做好准备
          </span>
        </header>
        <main className="mx-auto min-h-[78vh] max-w-[1000px] px-4 py-7 sm:px-7 sm:py-12">
          {children}
        </main>
        <footer className="p-6 text-center text-[13px] text-muted">
          一次专注的练习，让表达更从容。
        </footer>
      </body>
    </html>
  );
}
