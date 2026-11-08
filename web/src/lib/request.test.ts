import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api/errors";
import { createRequestRunner } from "./request";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((accept, fail) => {
    resolve = accept;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function harness() {
  let busy = false;
  let error: ApiError | null = null;
  const runner = createRequestRunner(
    (value) => {
      busy = value;
    },
    (value) => {
      error = value;
    },
  );
  return {
    runner,
    get busy() {
      return busy;
    },
    get error() {
      return error;
    },
  };
}

describe("请求取消与状态复位", () => {
  it("cancel 立即清除 busy，忽略不响应取消的旧请求结果且不设置 error", async () => {
    const h = harness();
    const response = deferred<string>();
    const onSuccess = vi.fn();
    let signal: AbortSignal | undefined;
    const pending = h.runner.run((s) => {
      signal = s;
      return response.promise;
    }, onSuccess);
    expect(h.busy).toBe(true);
    h.runner.cancel();
    expect(signal?.aborted).toBe(true);
    expect(h.busy).toBe(false);
    expect(h.error).toBeNull();
    response.resolve("旧结果");
    await pending;
    expect(onSuccess).not.toHaveBeenCalled();
    expect(h.busy).toBe(false);
    expect(h.error).toBeNull();
  });

  it("请求因 abort 拒绝时保持 idle，不显示错误", async () => {
    const h = harness();
    const onSuccess = vi.fn();
    const pending = h.runner.run(
      (signal) =>
        new Promise<string>((_, reject) => {
          signal.addEventListener("abort", () => reject(signal.reason), {
            once: true,
          });
        }),
      onSuccess,
    );
    h.runner.cancel();
    await pending;
    expect(h.busy).toBe(false);
    expect(h.error).toBeNull();
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it("取消后启动新请求，旧请求的 finally 不得清除新请求的 busy", async () => {
    const h = harness();
    const old = deferred<string>();
    const current = deferred<string>();
    const onOldSuccess = vi.fn();
    const onCurrentSuccess = vi.fn();
    const oldPending = h.runner.run(() => old.promise, onOldSuccess);
    h.runner.cancel();
    const currentPending = h.runner.run(
      () => current.promise,
      onCurrentSuccess,
    );
    old.resolve("旧结果");
    await oldPending;
    expect(h.busy).toBe(true);
    expect(onOldSuccess).not.toHaveBeenCalled();
    current.resolve("新结果");
    await currentPending;
    expect(onCurrentSuccess).toHaveBeenCalledWith("新结果");
    expect(h.busy).toBe(false);
  });

  it("旧请求晚到的错误不会覆盖新请求错误", async () => {
    const h = harness();
    const old = deferred<string>();
    const oldPending = h.runner.run(() => old.promise, vi.fn());
    h.runner.cancel();
    const failure = new ApiError("新错误", 422, "plan_invalid");
    await h.runner.run(() => Promise.reject(failure), vi.fn());
    old.reject(new TypeError("旧连接错误"));
    await oldPending;
    expect(h.error).toBe(failure);
    expect(h.busy).toBe(false);
    h.runner.clearError();
    expect(h.error).toBeNull();
  });
});
