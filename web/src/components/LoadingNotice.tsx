"use client";
import { useEffect, useState } from "react";
export function LoadingNotice({ message }: { message: string }) {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const start = Date.now();
    const timer = window.setInterval(
      () => setSeconds(Math.floor((Date.now() - start) / 1000)),
      1000,
    );
    return () => window.clearInterval(timer);
  }, []);
  return (
    <div
      role="status"
      aria-busy="true"
      className="flex items-center gap-4 rounded-xl bg-[#e9f3ed] p-[18px]"
    >
      <span
        className="size-5 shrink-0 animate-spin rounded-full border-2 border-[#b6d5c7] border-t-accent motion-reduce:animate-none"
        aria-hidden="true"
      />
      <span>
        {message}
        <br />
        <span className="text-muted">已等待 {seconds} 秒</span>
      </span>
    </div>
  );
}
