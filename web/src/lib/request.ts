import { ApiError, toTransportError } from "./api/errors";

// 与 React 无关的请求状态逻辑；取消后旧结果不能影响后续请求。
export function createRequestRunner(
  setBusy: (busy: boolean) => void,
  setError: (error: ApiError | null) => void,
) {
  let active: AbortController | null = null;
  const clearError = () => setError(null);
  const cancel = () => {
    active?.abort();
    active = null;
    setBusy(false);
  };
  const run = async <T>(
    operation: (signal: AbortSignal) => Promise<T>,
    onSuccess: (value: T) => void,
  ) => {
    if (active) return;
    const controller = new AbortController();
    active = controller;
    setBusy(true);
    setError(null);
    try {
      const value = await operation(controller.signal);
      if (active === controller && !controller.signal.aborted) onSuccess(value);
    } catch (cause) {
      const failure = toTransportError(cause, controller.signal);
      if (active === controller && failure.code !== "aborted")
        setError(failure);
    } finally {
      if (active === controller) {
        active = null;
        setBusy(false);
      }
    }
  };
  return { run, cancel, clearError };
}
