import createClient from "openapi-fetch";
import type { components, paths } from "./schema";
import { ApiError, toApiError, toTransportError } from "./errors";

// 单次模型调用可用 30 秒，结构化输出最多重试两次；上传还包含文字抽取。
// 创建面试串行解析 JD、生成计划，因此为两段调用及数据库读写留出时间。
export const TIMEOUT_MS = {
  resume: 120_000,
  jd: 90_000,
  create: 240_000,
  standard: 15_000,
} as const;
const client = createClient<paths>({
  baseUrl: process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000",
  fetch: async (request) => {
    const response = await globalThis.fetch(request);
    if (!response.ok) {
      const raw = await response.text();
      let body: unknown = raw;
      try {
        body = JSON.parse(raw);
      } catch {
        /* 网关错误可能不是 JSON。 */
      }
      throw toApiError(response.status, body, response.headers);
    }
    return response;
  },
});

async function request<T>(
  timeout: number,
  callerSignal: AbortSignal | undefined,
  operation: (signal: AbortSignal) => Promise<{ data?: T; response: Response }>,
): Promise<T> {
  const timer = AbortSignal.timeout(timeout);
  const signal = callerSignal ? AbortSignal.any([callerSignal, timer]) : timer;
  try {
    const { data, response } = await operation(signal);
    if (data === undefined)
      throw new ApiError(
        "后端返回了空响应，请稍后重试",
        response.status,
        "invalid_response",
        response.headers.get("X-Request-ID"),
      );
    return data;
  } catch (error) {
    throw toTransportError(error, signal);
  }
}

export function uploadResume(file: File, signal?: AbortSignal) {
  return request(TIMEOUT_MS.resume, signal, (combined) =>
    client.POST("/api/resumes", {
      signal: combined,
      body: { file: file.name },
      bodySerializer: () => {
        const body = new FormData();
        body.append("file", file);
        return body;
      },
    }),
  );
}
export function getResume(id: string, signal?: AbortSignal) {
  return request(TIMEOUT_MS.standard, signal, (combined) =>
    client.GET("/api/resumes/{resume_id}", {
      params: { path: { resume_id: id } },
      signal: combined,
    }),
  );
}
export function updateResume(
  id: string,
  body: components["schemas"]["ResumeReplacement"],
  signal?: AbortSignal,
) {
  return request(TIMEOUT_MS.standard, signal, (combined) =>
    client.PATCH("/api/resumes/{resume_id}", {
      params: { path: { resume_id: id } },
      body,
      signal: combined,
    }),
  );
}
export function parseJd(text: string, signal?: AbortSignal) {
  return request(TIMEOUT_MS.jd, signal, (combined) =>
    client.POST("/api/jd/parse", { body: { text }, signal: combined }),
  );
}
export function createInterview(
  body: components["schemas"]["InterviewCreate"],
  signal?: AbortSignal,
) {
  return request(TIMEOUT_MS.create, signal, (combined) =>
    client.POST("/api/interviews", { body, signal: combined }),
  );
}
export function getPlan(id: string, signal?: AbortSignal) {
  return request(TIMEOUT_MS.standard, signal, (combined) =>
    client.GET("/api/interviews/{interview_id}/plan", {
      params: { path: { interview_id: id } },
      signal: combined,
    }),
  );
}
export function updatePlan(
  id: string,
  questions: components["schemas"]["PlanUpdate"]["questions"],
  signal?: AbortSignal,
) {
  return request(TIMEOUT_MS.standard, signal, (combined) =>
    client.PATCH("/api/interviews/{interview_id}/plan", {
      params: { path: { interview_id: id } },
      body: { questions },
      signal: combined,
    }),
  );
}

export function getReport(id: string, signal?: AbortSignal) {
  return request(TIMEOUT_MS.standard, signal, combined =>
    client.GET("/api/interviews/{interview_id}/report", {
      params: { path: { interview_id: id } },
      signal: combined,
    }),
  );
}

export function regenerateReport(id: string, signal?: AbortSignal) {
  return request(TIMEOUT_MS.standard, signal, combined =>
    client.POST("/api/interviews/{interview_id}/report", {
      params: { path: { interview_id: id } },
      signal: combined,
    }),
  );
}

export function getTranscript(id: string, signal?: AbortSignal) {
  return request(TIMEOUT_MS.standard, signal, combined =>
    client.GET("/api/interviews/{interview_id}/transcript", {
      params: { path: { interview_id: id } },
      signal: combined,
    }),
  );
}

export function createVoiceSession(id: string, signal?: AbortSignal) {
  return request(TIMEOUT_MS.standard, signal, combined =>
    client.POST("/api/interviews/{interview_id}/session", {
      params: { path: { interview_id: id } },
      signal: combined,
    }),
  );
}
