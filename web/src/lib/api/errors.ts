export class ApiError extends Error {
  constructor(
    message: string,
    public status: number | null,
    public code: string,
    public traceId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : null;
}

export function toApiError(
  status: number,
  body: unknown,
  headers: Headers,
): ApiError {
  const error = record(record(body)?.error);
  const string = (value: unknown) =>
    typeof value === "string" && value.trim() ? value : null;
  return new ApiError(
    string(error?.message) ?? "请求失败，请稍后重试",
    status,
    string(error?.code) ?? `http_${status}`,
    string(error?.trace_id) ?? headers.get("X-Request-ID"),
  );
}

export function toTransportError(
  cause: unknown,
  signal?: AbortSignal,
): ApiError {
  if (cause instanceof ApiError) return cause;
  const reason: unknown = signal?.aborted ? signal.reason : cause;
  const name = reason instanceof Error ? reason.name : "";
  if (name === "TimeoutError")
    return new ApiError("请求超时，请稍后重试", null, "timeout");
  if (signal?.aborted || name === "AbortError")
    return new ApiError("请求已取消", null, "aborted");
  return new ApiError(
    "连不上后端服务，请确认已运行 make api；若后端日志显示请求已成功，检查 .env 的 WEB_ORIGINS 是否包含当前网页地址",
    null,
    "network_error",
  );
}
