import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "面试练习室",
  description: "用你的简历和目标岗位练一场模拟面试，结束后拿到带原话依据的评估报告。",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" className="h-full">
      <body className="flex min-h-full flex-col bg-paper text-ink antialiased">
        <header className="border-b border-line">
          <div className="mx-auto flex max-w-4xl items-baseline justify-between gap-4 px-4 py-4 sm:px-8">
            <Link
              href="/new"
              className="font-serif text-lg font-semibold tracking-wide text-ink"
            >
              面试练习室
            </Link>
            <Link
              href="/new"
              className="text-sm text-muted underline-offset-4 hover:text-ink hover:underline"
            >
              新的练习
            </Link>
          </div>
        </header>

        <main className="mx-auto w-full max-w-4xl flex-1 px-4 py-10 sm:px-8 sm:py-14">
          {children}
        </main>

        <footer className="border-t border-line py-6 text-xs text-faint">
          <p className="mx-auto max-w-4xl px-4 sm:px-8">
            一次专注的练习，让表达更从容。
          </p>
        </footer>
      </body>
    </html>
  );
}
