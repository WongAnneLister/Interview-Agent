import type { Metadata } from "next";
export const metadata: Metadata = { title: "文字面试 · 面试练习室" };
export default function TextInterviewLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
