"use client";

import { buttonSecondary } from "@/lib/styles";
import { useRef, useState } from "react";
import type { ApiError } from "@/lib/api/errors";

export function ErrorNotice({
  error,
  onRetry,
}: {
  error: ApiError | null;
  onRetry?: () => void;
}) {
  const trace = useRef<HTMLSpanElement>(null);
  const [copyStatus, setCopyStatus] = useState("");

  if (!error || error.code === "aborted") return null;

  async function copy() {
    try {
      await navigator.clipboard.writeText(error?.traceId ?? "");
      setCopyStatus("已复制");
      setTimeout(() => setCopyStatus(""), 2000);
    } catch {
      if (trace.current) {
        const range = document.createRange();
        range.selectNodeContents(trace.current);
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(range);
      }
      setCopyStatus("已选中编号，请手动复制");
    }
  }

  return (
    <div role="alert" className="flex min-w-0 flex-col gap-2 border-l-2 border-danger pl-4">
      <p className="leading-relaxed text-danger-ink">{error.message}</p>
      {error.traceId && (
        <p className="flex flex-wrap items-center gap-x-2 text-xs text-muted">
          <span>排查编号</span>
          <code className="font-mono text-ink-soft">
            <span ref={trace}>{error.traceId}</span>
          </code>
          <button
            type="button"
            className="cursor-pointer text-muted underline-offset-4 hover:text-ink hover:underline"
            onClick={copy}
          >
            {copyStatus || "复制"}
          </button>
        </p>
      )}
      {onRetry && (
        <div className="pt-1">
          <button type="button" className={buttonSecondary} onClick={onRetry}>
            重试
          </button>
        </div>
      )}
    </div>
  );
}
