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
    <div
      role="alert"
      className="flex min-w-0 flex-col gap-4 rounded-xl border border-[#e6bbb0] bg-[#fff2ed] p-5"
    >
      <p>{error.message}</p>
      {error.traceId && (
        <p className="text-xs wrap-anywhere">
          错误编号：<span ref={trace}>{error.traceId}</span>{" "}
          <button className={buttonSecondary} onClick={copy}>
            复制编号
          </button>
        </p>
      )}
      {copyStatus && <p role="status">{copyStatus}</p>}
      {onRetry && (
        <div>
          <button className={buttonSecondary} onClick={onRetry}>
            重试
          </button>
        </div>
      )}
    </div>
  );
}
