import { describe, expect, it } from "vitest";
import { ApiError, toApiError, toTransportError } from "./errors";

describe("API 错误", () => {
  it("保留后端业务信息和 trace_id", () => {
    const error = toApiError(
      422,
      {
        error: {
          code: "plan_invalid",
          message: "缺少能力项",
          trace_id: "body-id",
        },
      },
      new Headers({ "X-Request-ID": "header-id" }),
    );
    expect(error).toBeInstanceOf(ApiError);
    expect([error.status, error.code, error.message, error.traceId]).toEqual([
      422,
      "plan_invalid",
      "缺少能力项",
      "body-id",
    ]);
  });
  it.each([
    "<html>bad gateway</html>",
    null,
    {},
    { error: { code: 3, message: [] } },
  ])("不泄露非标准错误体 %j", (body) => {
    const error = toApiError(
      502,
      body,
      new Headers({ "X-Request-ID": "fallback" }),
    );
    expect(error.message).toBe("请求失败，请稍后重试");
    expect(error.traceId).toBe("fallback");
  });
  it("缺失 trace_id 时读取响应头", () => {
    expect(
      toApiError(
        404,
        { error: { code: "not_found", message: "面试不存在" } },
        new Headers({ "x-request-id": "id" }),
      ).traceId,
    ).toBe("id");
  });
  it("没有可用编号时返回 null", () => {
    expect(toApiError(500, { error: {} }, new Headers()).traceId).toBeNull();
  });
  it.each([
    [
      new TypeError("Failed to fetch"),
      "network_error",
      "连不上后端服务，请确认已运行 make api；若后端日志显示请求已成功，检查 .env 的 WEB_ORIGINS 是否包含当前网页地址",
    ],
    [new DOMException("", "TimeoutError"), "timeout", "请求超时，请稍后重试"],
    [new DOMException("", "AbortError"), "aborted", "请求已取消"],
  ])("归一化网络、超时、取消", (cause, code, message) => {
    const error = toTransportError(cause);
    expect([error.status, error.code, error.message]).toEqual([
      null,
      code,
      message,
    ]);
  });
  it("组合取消信号的原因优先于 fetch 的 TypeError", () => {
    expect(
      toTransportError(
        new TypeError(),
        AbortSignal.abort(new DOMException("", "TimeoutError")),
      ).code,
    ).toBe("timeout");
  });
});
