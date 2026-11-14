import type { Metadata } from "next";
export const metadata: Metadata = { title: "语音面试 · 面试练习室" };
export default function VoiceInterviewLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
