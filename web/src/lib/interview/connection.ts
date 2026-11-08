import type { components } from "../api/schema";
import { parseServerMessage, type Connection } from "./session";
type ServerMessage = components["schemas"]["TextServerMessage"];
type ClientMessage = components["schemas"]["TextClientMessage"];
export interface Socket {
  readyState: number;
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onclose: ((event: CloseEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  send(raw: string): void;
  close(): void;
}
type Timers = Pick<
  typeof globalThis,
  "setTimeout" | "clearTimeout" | "setInterval" | "clearInterval"
>;
// 刷新页面后，服务端清理旧连接（写库、等后台评估）最长约 10 秒，期间新连接会收到 4409。
// 从没连上过时先按 1/2/4/8 秒静默重试约 15 秒，仍被占用才判定为在其他窗口打开。
export const FIRST_BUSY_RETRIES = 4;
export function closeDecision(
  code: number,
  context: { everConnected: boolean; ended: boolean; attempt: number },
): "report" | "fail" | "retry" {
  if (context.ended || code === 4410) return "report";
  if (code === 4404) return "fail";
  if (
    code === 4409 &&
    !context.everConnected &&
    context.attempt >= FIRST_BUSY_RETRIES
  )
    return "fail";
  return "retry";
}
export function createConnection({
  url,
  onMessage,
  onStatus,
  WebSocketImpl = WebSocket,
  timers = globalThis,
}: {
  url: string;
  onMessage: (message: ServerMessage) => void;
  onStatus: (status: Connection, attempt: number) => void;
  WebSocketImpl?: new (url: string) => Socket;
  timers?: Timers;
}) {
  let socket: Socket | null = null;
  let stopped = false;
  let ended = false;
  let everConnected = false;
  let attempt = 0;
  let heartbeat: ReturnType<typeof setInterval> | undefined;
  let retry: ReturnType<typeof setTimeout> | undefined;
  // 被拒绝的连接会先发一条 error 再关闭；只有最终放弃时才交给调用方显示。
  let rejection: ServerMessage | null = null;
  function stopHeartbeat() {
    timers.clearInterval(heartbeat);
    heartbeat = undefined;
  }
  function send(message: ClientMessage): boolean {
    if (stopped || ended || socket?.readyState !== 1) return false;
    try {
      socket.send(JSON.stringify(message));
      return true;
    } catch {
      return false;
    }
  }
  function disconnected(code: number) {
    stopHeartbeat();
    if (stopped) return;
    const decision = closeDecision(code, { everConnected, ended, attempt });
    if (decision === "report") {
      ended = true;
      onStatus("ended", attempt);
      return;
    }
    if (decision === "fail" || attempt >= 10) {
      if (rejection) onMessage(rejection);
      rejection = null;
      onStatus("failed", attempt);
      return;
    }
    const delay = Math.min(1000 * 2 ** attempt, 10000);
    attempt += 1;
    onStatus("reconnecting", attempt);
    retry = timers.setTimeout(connect, delay);
  }
  function connect() {
    if (stopped || ended) return;
    let current: Socket;
    try {
      current = new WebSocketImpl(url);
    } catch {
      disconnected(1006);
      return;
    }
    socket = current;
    let confirmed = false;
    current.onopen = () => {
      if (stopped || current !== socket) return;
      onStatus("open", attempt);
      heartbeat = timers.setInterval(() => {
        send({ type: "ping" });
      }, 20000);
    };
    current.onmessage = ({ data }) => {
      if (stopped || current !== socket || typeof data !== "string") return;
      const message = parseServerMessage(data);
      if (!message) return;
      // 拒绝连接也会先 accept；只有会话事件证明连接真正恢复。
      if (message.type === "error" && !confirmed) {
        rejection = message;
        return;
      }
      if (message.type !== "error" && message.type !== "pong") {
        confirmed = true;
        everConnected = true;
        rejection = null;
        if (attempt > 0) {
          attempt = 0;
          onStatus("open", 0);
        }
      }
      if (message.type === "ended") {
        ended = true;
        stopHeartbeat();
      }
      onMessage(message);
      if (ended) onStatus("ended", attempt);
    };
    current.onclose = ({ code }) => {
      if (current === socket) disconnected(code);
    };
    current.onerror = () => {
      /* 浏览器随后触发 close，由关闭码统一决策。 */
    };
  }
  onStatus("connecting", 0);
  connect();
  return {
    send,
    close() {
      stopped = true;
      timers.clearTimeout(retry);
      stopHeartbeat();
      socket?.close();
    },
  };
}
