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

  const text = message.replace(/[…。]+$/, "");
  return (
    <div
      role="status"
      aria-busy="true"
      className="flex items-baseline justify-between gap-4 border-l-2 border-accent pl-4"
    >
      <p className="text-ink-soft">
        {text}
        <span className="ml-0.5 motion-safe:animate-pulse">…</span>
        <span className="mt-0.5 block text-xs text-muted">请不要关闭或刷新页面</span>
      </p>
      <span className="shrink-0 font-serif text-muted">{seconds} 秒</span>
    </div>
  );
}
