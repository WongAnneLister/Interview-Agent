import { afterEach, expect, it, vi } from "vitest";
import {
  closeDecision,
  createConnection,
  FIRST_BUSY_RETRIES,
  type Socket,
} from "./connection";

class FakeSocket implements Socket {
  static instances: FakeSocket[] = [];
  readyState = 0;
  onopen: Socket["onopen"] = null;
  onmessage: Socket["onmessage"] = null;
  onclose: Socket["onclose"] = null;
  onerror: Socket["onerror"] = null;
  sent: string[] = [];
  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  send(raw: string) {
    this.sent.push(raw);
  }
  close() {
    this.readyState = 3;
    this.onclose?.({ code: 1000 } as CloseEvent);
  }
  open() {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }
  message(raw: string) {
    this.onmessage?.(new MessageEvent("message", { data: raw }));
  }
  drop(code = 1006) {
    this.readyState = 3;
    this.onclose?.({ code } as CloseEvent);
  }
}
function setup() {
  vi.useFakeTimers();
  FakeSocket.instances = [];
  const onStatus = vi.fn();
  const onMessage = vi.fn();
  const connection = createConnection({
    url: "ws://test",
    WebSocketImpl: FakeSocket,
    timers: globalThis,
    onStatus,
    onMessage,
  });
  const socket = () => FakeSocket.instances.at(-1)!;
  return { ...connection, socket, onStatus, onMessage };
}
afterEach(() => vi.useRealTimers());

it.each([
  [1000, false, false, 0, "retry"],
  [1000, false, true, 0, "report"],
  [4410, false, false, 0, "report"],
  [4404, true, false, 0, "fail"],
  [4409, false, false, 0, "retry"],
  [4409, false, false, FIRST_BUSY_RETRIES - 1, "retry"],
  [4409, false, false, FIRST_BUSY_RETRIES, "fail"],
  [4409, true, false, FIRST_BUSY_RETRIES, "retry"],
  [4408, true, false, 0, "retry"],
  [1011, true, false, 0, "retry"],
  [1006, false, false, 0, "retry"],
  [4999, true, false, 0, "retry"],
] as const)(
  "关闭决策 %s %s %s 第 %s 次",
  (code, everConnected, ended, attempt, expected) => {
    expect(closeDecision(code, { everConnected, ended, attempt })).toBe(
      expected,
    );
  },
);
it("20 秒心跳，离线发送失败且不缓存，主动关闭停止所有定时器", () => {
  const c = setup();
  expect(c.send({ type: "user_text", text: "test" })).toBe(false);
  c.socket().open();
  vi.advanceTimersByTime(19999);
  expect(c.socket().sent).toEqual([]);
  vi.advanceTimersByTime(1);
  expect(c.socket().sent).toEqual(['{"type":"ping"}']);
  c.close();
  vi.advanceTimersByTime(100000);
  expect(FakeSocket.instances).toHaveLength(1);
  expect(vi.getTimerCount()).toBe(0);
});
it("退避为 1/2/4/8/10 秒，最多自动重试 10 次", () => {
  const c = setup();
  for (const [i, delay] of [
    1000, 2000, 4000, 8000, 10000, 10000, 10000, 10000, 10000, 10000,
  ].entries()) {
    c.socket().open(); // 被拒绝的握手也会 open，不能清零重试计数
    c.socket().drop(1006);
    expect(c.onStatus).toHaveBeenLastCalledWith("reconnecting", i + 1);
    vi.advanceTimersByTime(delay - 1);
    expect(FakeSocket.instances).toHaveLength(i + 1);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.instances).toHaveLength(i + 2);
  }
  c.socket().drop();
  expect(c.onStatus).toHaveBeenLastCalledWith("failed", 10);
  expect(vi.getTimerCount()).toBe(0);
});
it("首次连接被 4409 拒绝时静默重试约 15 秒，仍被占用才失败并给出原因", () => {
  const c = setup();
  const reject = () => {
    c.socket().open();
    c.socket().message(
      '{"type":"error","code":"already_connected","message":"占用","trace_id":"trace"}',
    );
    c.socket().drop(4409);
  };
  for (const [i, delay] of [1000, 2000, 4000, 8000].entries()) {
    reject();
    expect(c.onStatus).toHaveBeenLastCalledWith("reconnecting", i + 1);
    expect(c.onMessage).not.toHaveBeenCalled();
    vi.advanceTimersByTime(delay);
  }
  reject();
  expect(c.onStatus).toHaveBeenLastCalledWith("failed", FIRST_BUSY_RETRIES);
  expect(c.onMessage).toHaveBeenCalledTimes(1);
  expect(c.onMessage).toHaveBeenCalledWith(
    expect.objectContaining({ trace_id: "trace" }),
  );
  expect(vi.getTimerCount()).toBe(0);
});
it("刷新后旧连接清理完毕，静默重试成功且不显示拒绝原因", () => {
  const c = setup();
  c.socket().open();
  c.socket().message(
    '{"type":"error","code":"already_connected","message":"占用","trace_id":"trace"}',
  );
  c.socket().drop(4409);
  vi.advanceTimersByTime(1000);
  c.socket().open();
  c.socket().message('{"type":"state","value":"listening"}');
  expect(c.onStatus).toHaveBeenLastCalledWith("open", 0);
  expect(c.onMessage).toHaveBeenCalledTimes(1);
  expect(c.onMessage).toHaveBeenCalledWith({ type: "state", value: "listening" });
});
it("自动重连中的 4409 继续退避，即使此前没有完成握手", () => {
  const c = setup();
  c.socket().drop();
  vi.advanceTimersByTime(1000);
  c.socket().open();
  c.socket().drop(4409);
  expect(c.onStatus).toHaveBeenLastCalledWith("reconnecting", 2);
});
it("有效会话事件清零重试次数，恢复后 4409 继续重连", () => {
  const c = setup();
  c.socket().drop();
  vi.advanceTimersByTime(1000);
  c.socket().open();
  c.socket().message('{"type":"state","value":"listening"}');
  c.socket().drop();
  expect(c.onStatus).toHaveBeenLastCalledWith("reconnecting", 1);
  vi.advanceTimersByTime(1000);
  c.socket().open();
  c.socket().drop(4409);
  expect(c.onStatus).toHaveBeenLastCalledWith("reconnecting", 2);
});
it("ended 或 4410 结束连接且无重试，未知消息安全忽略", () => {
  const c = setup();
  c.socket().open();
  c.socket().message("bad json");
  expect(c.onMessage).not.toHaveBeenCalled();
  c.socket().message('{"type":"ended"}');
  c.socket().drop(1000);
  expect(c.onStatus).toHaveBeenLastCalledWith("ended", 0);
  expect(vi.getTimerCount()).toBe(0);
});
