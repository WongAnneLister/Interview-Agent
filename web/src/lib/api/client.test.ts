import { afterEach, expect, it, vi } from "vitest";
import {
  createInterview,
  getPlan,
  getResume,
  parseJd,
  TIMEOUT_MS,
  updatePlan,
  updateResume,
  uploadResume,
} from "./client";
import { defaultSettings, resumeToDraft } from "../setup";
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
it("上传真实 File 为 multipart file，浏览器设置 boundary", async () => {
  const file = new File(["%PDF-1.4"], "resume.pdf", {
    type: "application/pdf",
  });
  const fetch = vi.fn(async (request: Request) => {
    expect(request.headers.get("Content-Type")).toMatch(
      /^multipart\/form-data; boundary=/,
    );
    const uploaded = (await request.formData()).get("file");
    expect(uploaded).toBeInstanceOf(File);
    expect(await (uploaded as File).text()).toBe("%PDF-1.4");
    return Response.json({ id: "id", parsed: { name: "候选人" } });
  });
  vi.stubGlobal("fetch", fetch);
  expect((await uploadResume(file)).id).toBe("id");
  expect(fetch).toHaveBeenCalledTimes(1);
});
it("所有业务请求使用各自超时且不重试付费请求", async () => {
  const timeout = vi.spyOn(AbortSignal, "timeout");
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json({})),
  );
  await uploadResume(new File([], "cv.pdf"));
  await parseJd("JD");
  await createInterview({ ...defaultSettings, resume_id: "id", jd_text: "JD" });
  await getResume("id");
  await updateResume("id", resumeToDraft({ name: "" }));
  await getPlan("id");
  await updatePlan("id", []);
  expect(timeout.mock.calls.map((call) => call[0])).toEqual([
    TIMEOUT_MS.resume,
    TIMEOUT_MS.jd,
    TIMEOUT_MS.create,
    ...Array(4).fill(TIMEOUT_MS.standard),
  ]);
});
it("非 JSON 错误转换为带响应头 traceId 的 ApiError", async () => {
  const fetch = vi.fn(
    async () =>
      new Response("<html>gateway error</html>", {
        status: 502,
        headers: { "X-Request-ID": "gateway-id" },
      }),
  );
  vi.stubGlobal("fetch", fetch);
  await expect(parseJd("JD")).rejects.toMatchObject({
    status: 502,
    code: "http_502",
    traceId: "gateway-id",
  });
  expect(fetch).toHaveBeenCalledTimes(1);
});
it("调用方取消信号传到请求，并转换为 aborted", async () => {
  const caller = new AbortController();
  vi.stubGlobal(
    "fetch",
    vi.fn(
      (request: Request) =>
        new Promise<Response>((_, reject) => {
          request.signal.addEventListener(
            "abort",
            () => reject(request.signal.reason),
            { once: true },
          );
        }),
    ),
  );
  const result = getPlan("id", caller.signal);
  caller.abort();
  await expect(result).rejects.toMatchObject({ status: null, code: "aborted" });
});
